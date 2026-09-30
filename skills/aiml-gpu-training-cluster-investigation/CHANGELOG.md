# Changelog

## 1.0.1

Addresses the TFC SME review on PR #112. Every value below was re-verified against the
NVIDIA Xid catalog and the live SageMaker API rather than carried over from the review.

- Xid 48 now splits on which memory faulted. Added Xid 171 (`UNCORRECTABLE_DRAM_ERROR`)
  and 172 (`UNCORRECTABLE_SRAM_ERROR`) as qualifiers, plus routing rule 6: DRAM follows the
  reboot-and-retire path, SRAM checks the SRAM double-bit threshold flag and replaces if it
  is set, and an undetermined case is reported `UNVERIFIED` instead of defaulting to reboot.
  This was the one review item that could produce a wrong reboot-versus-replace verdict
- Added the NVLink 5 Xid family 144 to 150, Blackwell only, which is the hardware the skill
  targets for `p6-b200` and `p6-b300`. `WORKFLOW_NVLINK5_ERR` has no fixed verdict: it
  requires decoding `intrInfo` and `errorStatus`. Rule 10 therefore parses the readable
  message fields (sub component, fatal versus nonfatal, link) and marks the precise
  resolution `UNVERIFIED` rather than inventing a replace recommendation
- Added Xid 137 (`NVLINK_PRIV_ERR`) and rule 9: an illegal peer-to-peer access that presents
  as NVLink but is an application bug, immediate action IGNORE. Classified with 13 and 31
- Added Xid 11, 25, and 32 to the application class, and Xid 157 with the note that its
  immediate action is IGNORE while its investigatory action is CONTACT_SUPPORT
- Added `sagemaker.ListClusterEvents` and `DescribeClusterEvent` as an eighth timeline
  source, reached first when log delivery is broken. Gated on `NodeProvisioningMode` being
  `Continuous`, verified live: other clusters return
  `ValidationException: ListClusterEvents is only supported for cluster with
  NodeProvisioningMode set to Continuous`. The response carries no severity field, so the
  skill is told not to report one
- Added the `Capacity Reservation Instance Interruption Warning` event as per-instance proof
  of a Capacity Block termination, with `instance-termination-time` and
  `instance-lifecycle`, in place of inferring it from the reservation `EndDate`

## 1.0.0

- Initial version: GPU evidence coverage audit, node verdicts against an NVIDIA and AWS
  evidence bar, Proven/Hypothesis cause labels, NCCL/NVLink/EFA checks, instance-generic
  capability profile, frequent non-GPU edge cases, and pre-flight readiness checks P1 to P16
  for SageMaker HyperPod (Slurm and EKS), AWS ParallelCluster, and self-managed GPU clusters
