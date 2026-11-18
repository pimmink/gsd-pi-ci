#!/usr/bin/env python3
"""One exact D014 run, <=15 minute native-job queue; never dispatch or retry a build."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

from native_cmux import CI_REPO, SOURCE_SHA, WORKFLOW, require, tag_ok

NATIVE_JOB = "native-build (macos-26-intel, x86_64)"
QUEUE_SECONDS = 15 * 60
POLL_SECONDS = 10
CANCEL_MARGIN = 60  # Reserve a minute for bounded diagnostic/recheck/cancel API calls.


def timestamp(value):
    require(isinstance(value, str) and value.endswith("Z"), "Expected exact UTC created_at timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def title(tag):
    return f"cmux build-only | {SOURCE_SHA} | {tag}"


def validate_run(run, run_id, workflow_sha, created_at, tag):
    require(run.get("id") == run_id, "Wrong run ID")
    require(run.get("repository", {}).get("full_name") == CI_REPO, "Wrong repository")
    require(run.get("head_repository", {}).get("full_name") == CI_REPO, "Wrong head repository")
    require(run.get("path") == WORKFLOW, "Wrong workflow path")
    require(run.get("head_sha") == workflow_sha, "Wrong workflow HEAD (not the native source HEAD)")
    require(run.get("event") == "workflow_dispatch" and run.get("run_attempt") == 1, "Not an original manual run")
    require(run.get("created_at") == created_at, "Wrong creation time")
    require(run.get("display_title") == title(tag), "Wrong native source/tag binding")
    require(run.get("status") in ("queued", "in_progress", "waiting", "pending", "requested", "completed"), "Unknown workflow state")


def native_state(jobs, run_id, created_at):
    matches = [j for j in jobs if j.get("name") == NATIVE_JOB]
    require(len(matches) <= 1, "Ambiguous native build job")
    if not matches:
        return "not-visible"
    job = matches[0]
    require(job.get("run_id") == run_id and job.get("run_attempt") == 1, "Wrong job run/attempt")
    require(job.get("labels") == ["macos-26-intel"], "Unexpected native runner labels")
    # GitHub sometimes fills started_at while status is still queued. That is NOT a start.
    # Nor is an auxiliary watchdog job or the workflow's overall `in_progress` status.
    if job.get("status") in ("in_progress", "completed") and job.get("runner_id", 0) > 0:
        started = job.get("started_at")
        step_starts = [s.get("started_at") for s in job.get("steps", [])
                       if s.get("status") in ("in_progress", "completed") and s.get("started_at")]
        if started and step_starts:
            require(timestamp(started) >= timestamp(created_at), "Native start predates exact run")
            require(all(timestamp(s) >= timestamp(created_at) for s in step_starts), "Step start predates exact run")
            return "started"
    return "not-started"


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, suffix):
        # No caller-selected repository, host or cancellation target.
        req = urllib.request.Request(f"https://api.github.com/repos/{CI_REPO}/{suffix}",
                                     data=b"" if method == "POST" else None, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                body = response.read(2 * 1024 * 1024)
                return json.loads(body) if body else {"http_status": response.status}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitHub {method} returned HTTP {exc.code}; no automatic retry") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError(f"GitHub {method} outcome unknown (network); no automatic retry") from None

    def snapshot(self, run_id):
        run = self.request("GET", f"actions/runs/{run_id}")
        jobs = self.request("GET", f"actions/runs/{run_id}/attempts/1/jobs?per_page=100")
        require(jobs.get("total_count", 101) <= 100, "Unexpected job pagination; refusing incomplete diagnosis")
        return run, jobs["jobs"]

    def approvals(self, run_id):
        return self.request("GET", f"actions/runs/{run_id}/pending_deployments")

    def cancel(self, run_id):
        return self.request("POST", f"actions/runs/{run_id}/cancel")


def write(path, data):
    content = json.dumps(data, indent=2) + "\n"
    require(len(content.encode()) <= 64 * 1024, "Unexpectedly large queue diagnosis; refusing unbounded artifact")
    path.write_text(content)


def watch(api, run_id, workflow_sha, created_at, tag, output, clock=time.time, sleep=time.sleep, observe_only=False):
    require(isinstance(run_id, int) and run_id > 0, "Invalid run ID")
    require(re.fullmatch(r"[0-9a-f]{40}", workflow_sha), "Full workflow SHA required")
    require(tag_ok(tag), "Invalid exclusive tag")
    created = timestamp(created_at)
    # The sole external parent guard must attach promptly, never revive history.
    require(0 <= clock() - created < QUEUE_SECONDS - CANCEL_MARGIN,
            "Attach before the cancellation safety margin; historical/late run refused")
    output.mkdir(parents=True, exist_ok=True)
    require(not (output / "result.json").exists(), "Use a fresh evidence directory; do not overwrite a prior watch")
    deadline = created + QUEUE_SECONDS
    result = {"repo": CI_REPO, "run_id": run_id, "workflow_sha": workflow_sha,
              "source_sha": SOURCE_SHA, "created_at": created_at, "tag": tag,
              "deadline": datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
              "cancel_requested": False, "needs_parent": False,
              "authority": "read-only-internal-observer" if observe_only else "sole-external-parent-guard"}
    poll = 0

    def snapshot(label):
        nonlocal poll
        run, jobs = api.snapshot(run_id)
        # Persist public diagnostics FIRST, even if an identity mismatch prevents action.
        poll += 1
        require(poll <= 100, "Unexpected clock/poll count; stop rather than monitor indefinitely")
        write(output / f"{poll:03d}-{label}.json", {"observed_at": clock(), "run": run, "jobs": jobs})
        validate_run(run, run_id, workflow_sha, created_at, tag)
        state = native_state(jobs, run_id, created_at)
        heartbeat = {"repo": CI_REPO, "run_id": run_id, "workflow_sha": workflow_sha,
                     "source_sha": SOURCE_SHA, "created_at": created_at, "tag": tag,
                     "observed_at": clock(), "queue_age_seconds": clock() - created,
                     "native_state": state, "authority": result["authority"]}
        write(output / "heartbeat.json", heartbeat)
        if poll == 1 or poll % 6 == 0:
            print(f"run {run_id}: {result['authority']}; native={state}; age={int(clock() - created)}s", flush=True)
        started = None
        if state == "started":
            job = next(j for j in jobs if j.get("name") == NATIVE_JOB)
            # Even after allocation, job.started_at may retain a queued placeholder.
            # Use the first real step's time for the cap (conservative), never that placeholder.
            started = min((s["started_at"] for s in job["steps"] if s.get("started_at") and
                           s.get("status") in ("in_progress", "completed")), key=timestamp)
            require(timestamp(started) <= clock() + 5, "Native step start is in the future; clock/API state unknown")
        return run, state, started

    try:
        while True:
            run, state, started = snapshot("observe")
            if state == "started":
                # A late observed start must never be reported as a successful queue bound.
                late = timestamp(started) >= deadline
                result.update(outcome="native-start-after-deadline" if late else "native-job-actually-started",
                              native_started_at=started, needs_parent=late)
                break
            if run.get("status") == "completed":
                result.update(outcome="run-terminal-without-confirmed-native-start", needs_parent=True)
                break
            if clock() < deadline - CANCEL_MARGIN:
                sleep(min(POLL_SECONDS, deadline - CANCEL_MARGIN - clock()))
                continue
            # Begin one minute early to keep diagnostics + cancel within the 15 minute cap.
            # Network/server failure cannot guarantee cancellation; report unknown, never success.
            # Preserve job/runner/concurrency/approval diagnosis before any cancellation.
            diagnosis = {"native_state": state, "concurrency_group": "native-cmux-build-only-d014",
                         "cancel_in_progress": False, "approval_status": "unknown"}
            try:
                diagnosis["pending_deployments"] = api.approvals(run_id)
                diagnosis["approval_status"] = "API snapshot only; account capacity/approval cause not inferred"
            except RuntimeError as exc:
                diagnosis["approval_error"] = str(exc)
            write(output / "deadline-diagnosis.json", diagnosis)
            # Recheck exact identity and native start after diagnosis, immediately before POST.
            run, state, _ = snapshot("before-cancel")
            if state == "started" or run.get("status") == "completed":
                result.update(outcome="start-or-completion-raced-deadline; not-cancelled", needs_parent=True)
                break
            if observe_only:
                result.update(needs_parent=True, outcome="observe-only deadline; sole external parent guard required")
                break  # No POST in this role, even if the external guard is absent/dead.
            result.update(cancel_requested=True, needs_parent=True, outcome="deadline; cancellation-outcome-unknown")
            write(output / "cancel-intent.json", result)
            try:
                result["cancel_response"] = api.cancel(run_id)  # Exactly one POST, never force-cancel/retry.
                result["outcome"] = "deadline; cancellation-requested-not-confirmed"
            except RuntimeError as exc:
                result["cancel_error"] = str(exc)
            # A POST acceptance is not evidence that no build started in the race window.
            try:
                after_run, after_state, _ = snapshot("after-cancel")
                result.update(after_cancel_status=after_run.get("status"), after_cancel_native_state=after_state)
            except (RuntimeError, ValueError, KeyError) as exc:
                result["after_cancel_error"] = str(exc)
            result["parent_action"] = "STOP. Parent must ask ONE fallback with ask_user_questions; no scripted answer, redispatch or label fallback. Reconcile cancel/build-start race first."
            break
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        result.update(outcome="guard-incomplete; no-further-action", error=str(exc), needs_parent=True)
    write(output / "result.json", result)
    print(f"https://github.com/{CI_REPO}/actions/runs/{run_id}: {result['outcome']}", flush=True)
    if result["needs_parent"]:
        print(result.get("parent_action", "STOP. Parent intervention required; no dispatch/cancel retry performed."), flush=True)
    return 2 if result["needs_parent"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--created-at", required=True, help="Exact new run's REST created_at, UTC Z")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observe-only", action="store_true", help="Internal observer; no cancellation authority")
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        parser.error("GH_TOKEN/GITHUB_TOKEN is required; use existing authenticated session, never a new PAT")
    try:
        return watch(GitHub(token), args.run_id, args.workflow_sha, args.created_at, args.tag, args.output,
                     observe_only=args.observe_only)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Guard refused: {exc}; no build retry/dispatch performed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
