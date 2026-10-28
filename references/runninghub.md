# RunningHub integration

Checked against official docs on2026-10-28. Account entitlements, endpoints and custom-node versions can change.

## Credentials and region

Set RUNNINGHUB_API_KEY in the environment. Choose the account's actual origin through RUNNINGHUB_BASE_URL: https://www.runninghub.ai or https://www.runninghub.cn. The client accepts only these HTTPS origins; no silent account-region switching.

A new upload endpoint may accept a key rejected by a legacy endpoint. A successful workflow/account read does not prove all upload endpoints support the key. Diagnose endpoint version and region before declaring a key invalid.

## Endpoints

| Purpose | POST path | Main fields |
|---|---|---|
| Workflow | /api/openapi/getJsonApiFormat | apiKey, workflowId; data.prompt is API graph |
| Upload | /openapi/v2/media/upload/binary | Bearer header, multipart file; fileName or filename |
| Create | /task/openapi/create | apiKey, workflowId, workflow string, nodeInfoList, instanceType |
| Status | /task/openapi/status | apiKey, taskId; status string in data |
| Outputs | /task/openapi/outputs | apiKey, taskId; fileUrl/nodeId/costs |

The normal success code is0; some upload revisions use200. Region errors must not cause infinite alternate-endpoint loops.

## Compile and execute

Fetched graphs can contain credentials in third-party nodes, private prompts and upload references. Save .private.json outside the release tree. Inspection redacts common credential fields but is not a complete declassification tool.

The project file maps semantic roles to node/input/class triples. Check all bindings. Global overrides apply before per-segment overrides. output_nodes prunes unrelated output branches, preventing accidental execution of an enhancer merely present in a template.

T8 conditioning adaptation is opt-in and checks MiniMaxH3AudioConditioningT8. It uses ref_images.ref_image_0/1 and ref_videos.ref_video_0/1, clearing those slots before binding the current assets. Other conditioning conventions need their own adapter.

Concurrency occupies complete generation jobs, including queueing and tracking, not just simultaneous HTTP create calls. Use the user's actual entitlement. Shared-image uploads are content-hash deduplicated within an execution scope (origin, workflow, instance and hashed credential). Input fingerprints also cover that scope, frame count, fps and expected dimensions.

## Recovery

Create is sent once. Connection loss, HTTP5xx or success without taskId leaves SUBMISSION_UNKNOWN. Reconcile the provider task history, then attach a confirmed task; do not automatically submit a duplicate.

Known task IDs survive tracking errors/timeouts. Stop new submissions because an untracked task may still occupy a cloud slot; run again including unresolved jobs to poll them. Do not run multiple CLI processes against the same state directory. Successful files are hash-checked and reused. Changed inputs or execution scopes require a new segment id/state directory. A failed generation needs explicit --retry-failed after correcting the cause, with one submission per attempt.

Runtime graphs, caches, states and media belong in ignored project directories. The release contains no individual account ids, credentials, upload references or user media.

Official docs:
- https://www.runninghub.ai/runninghub-api-doc-en/api-425761094
- https://www.runninghub.ai/runninghub-api-doc-en/api-425761098
- https://www.runninghub.ai/runninghub-api-doc-en/api-425761093
- https://www.runninghub.ai/runninghub-api-doc-en/api-425761033
- https://www.runninghub.ai/runninghub-api-doc-en/api-425761034
