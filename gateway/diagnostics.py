"""Build bounded stand reports from deployment metadata and privacy-filtered logs."""
import datetime

from gateway import tasks
from gateway.commands import COMPONENTS, STANDS
from gateway.kubernetes import DiagnosticError
from gateway.redaction import log_summary, redact
from worker.run import TaskCancelled


def check_cancel(db, task_id):
    if tasks.cancellation_requested(db, task_id):
        raise TaskCancelled("Task cancelled")


def health(client, stand, cancelled=lambda: None):
    lines = [f"Stand: {stand} ({STANDS[stand]})",
             "Observed: " + datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")]
    for component in COMPONENTS:
        cancelled()
        deployment = client.deployment(stand, component)
        status = deployment.get("status", {})
        desired = deployment["spec"].get("replicas", 1)
        lines.append(f"{component}: {status.get('availableReplicas', 0)}/{desired} available; "
                     f"updated={status.get('updatedReplicas', 0)}")
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            lines.append(f"  Image: {redact(container['image'])}")
        for pod in client.pods(stand, deployment)[:3]:
            for container in pod.get("status", {}).get("containerStatuses", []):
                state = container.get("state", {})
                reason = next(iter(state), "unknown")
                detail = state.get(reason, {}).get("reason", "")
                lines.append(f"  Pod: {reason} {detail}; ready={container.get('ready',False)}; restarts={container.get('restartCount',0)}")
    lines.append("This checks rollout/runtime metadata, not full game behavior.")
    return "\n".join(lines)


def logs(client, stand, component, minutes, cancelled=lambda: None):
    cancelled()
    deployment = client.deployment(stand, component)
    pods = client.pods(stand, deployment)[:2]
    lines = [f"Log summary: {stand}/{component}, last {minutes}m; at most 2 pods and 200 lines per pod."]
    for index, pod in enumerate(pods, 1):
        cancelled()
        # The deployment owns the selected pod. Never accept arbitrary pod names.
        container = deployment["spec"]["template"]["spec"]["containers"][0]["name"]
        text = client.logs(stand, pod["metadata"]["name"], container, minutes)
        lines.append(f"Sample {index}:\n" + log_summary(text))
    if not pods:
        lines.append("No matching pods found.")
    return "\n".join(lines)


def process_one(db, client):
    item = tasks.claim(db, "operations")
    if item is None:
        return False
    cancel = lambda: check_cancel(db, item["update_id"])
    try:
        if item["kind"] in ("health", "changes"):
            report = health(client, item["target"], cancel)
            if item["kind"] == "changes":
                report += "\nThese are current revisions. Historical deployment differences are not collected yet."
        else:
            report = logs(client, item["target"], item["component"], item["window"], cancel)
            if item["kind"] == "analyze":
                report = health(client, item["target"], cancel) + "\n\n" + report
        cancel()
        if item["kind"] == "analyze":
            tasks.save_evidence(db, item["update_id"], report)
        else:
            tasks.finish(db, item["update_id"], report)
    except TaskCancelled:
        tasks.finish(db, item["update_id"], "Cancelled", "cancelled")
    except DiagnosticError as error:
        tasks.finish(db, item["update_id"], str(error), "failed")
    return True
