# Changelog

## 1.0.2

Verified the 1.0.1 Blackwell content against a live `p6-b300.48xlarge` (8 x NVIDIA B300
SXM6 AC, driver 595.91.07, CUDA 13.2) rather than leaving it documentation-only. Four
field-name corrections and three false-positive traps came out of it.

- Xid 48 rule 6 now quotes the real `nvidia-smi -q -d ECC` field names instead of
  describing them: `SRAM Threshold Exceeded` (under `Aggregate`, the RMA gate),
  `SRAM Uncorrectable Parity` and `SRAM Uncorrectable SEC-DED` (two counters, not one),
  `DRAM Uncorrectable`, and the `Aggregate Uncorrectable SRAM Sources` breakdown
  (L2 / SM / Microcontroller / PCIE / Other) that locates the faulting unit.
- Added three verified signals that were missing: `Unrepairable Memory: Yes` (REPLACE, the
  same condition Xid 157 reports from the other side), `Channel Repair Pending` and
  `TPC Repair Pending` (REBOOT, a staged but unapplied repair), and the
  `Bank Remap Availability Histogram` as a pre-failure signal, since exhausted remap
  capacity is what later surfaces as a remap failure or Xid 157.
- Confirmed Xid 171/172 are available in practice: the current Deep Learning AMI ships
  driver 595.91.07, well past the R565 the catalog pairs them with.
- Replaced the NVLink error-counter names with the ones the driver actually emits. The
  older `Replay Errors` / `Recovery Errors` / `CRC Errors` do **not** exist on 595.91.07;
  the real counters are `Malformed packet Errors`, `Buffer overrun Errors`, `Rx Errors`,
  `Rx remote Errors`, `Rx General Errors`, `Local link integrity Errors`, `Tx discards`,
  `Link recovery successful/failed/Total events`, `Effective Errors`, `Symbol Errors`.
- Recorded three readings that look like faults and are not, each of which would have
  produced a wrong finding: `FEC Errors - 0` is the corrected-codeword counter and read
  36,140,749,276 at boot; `Effective BER` and `Symbol BER` read `15e-255`, the
  floating-point floor rather than a high rate; and `Raw Errors` / `Raw BER` per lane were
  non-zero on a healthy node, so they are never evidence on their own.
- Added the `Fabric` section fields as the clearest fabric health check
  (`State: Completed`, `Status: Success`, `CliqueId`, `GPU Fabric GUID`), in preference to
  parsing Fabric Manager log lines.
- Documented that InfiniBand device count is not an EFA check on Blackwell. A B300 launched
  with no EFA interface still showed `ibp198s0f0` and `ibp199s0f0`, which are ConnectX
  bridge devices (`mlx5_core`, firmware 28.47.2526) used for NVLink subnet management.
- Documented that the 1800 GB/s NVSwitch figure is bidirectional while `nvidia-smi` reports
  per-link unidirectional (`NV18` at 53.125 GB/s, so 956.25 GB/s per direction), so the
  two must not be divided against each other to infer a degraded fabric.

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
