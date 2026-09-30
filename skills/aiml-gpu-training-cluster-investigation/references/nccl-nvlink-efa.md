# NCCL Transport, NVLink / NVSwitch, and EFA Signals

Where each GPU-communication signal can be seen, what a good and a bad value look like,
and what to do when it is not visible. Log strings are quoted from the sources linked in
each section. Do not paraphrase them into search patterns that match more than they say.

## 1. Which transport NCCL actually used

NCCL writes its transport choices only when `NCCL_DEBUG=INFO` (or higher) is set, and only
to the job's stdout or to `NCCL_DEBUG_FILE`. These reach CloudWatch only if the customer
ships job output. Search every log source found in SKILL.md Step 3a for `NCCL INFO` and
`NCCL WARN` first. **If there are no NCCL lines at all, NCCL transport is `Not observable`.**
Never infer "NCCL used EFA" from the instance type or the EFA security group.

| Log line | Meaning | Verdict |
|----------|---------|---------|
| `NET/OFI Selected Provider is efa` and `Using network AWS Libfabric` | Inter-node traffic goes over EFA through the AWS OFI NCCL plugin | Good |
| `Using network IB` | NCCL chose an InfiniBand-verbs network | Unexpected on EC2 EFA instances; report it |
| Channel lines `... via NET/Socket/<n>` | Inter-node traffic over TCP sockets | **Bad** on EFA instances: silent fallback. The AWS blog on P3dn measured about a three-fold bus-bandwidth gain for EFA over TCP |
| Channel lines `... via P2P/CUMEM` | Intra-node GPU to GPU by direct peer access (NVLink on NVSwitch nodes) | Good |
| `NVLS Creating Multicast group ...` | NVLink SHARP in use for collectives | Good on NVSwitch systems that support it |
| Channel lines `... via SHM/direct/direct` | Intra-node traffic through host shared memory | On an NVSwitch node, peer access is not being used; report as degraded |

Sources: [NCCL logging](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/logging.html),
[Training LLMs on SageMaker: best practices](https://aws.amazon.com/blogs/machine-learning/training-large-language-models-on-amazon-sagemaker-best-practices/),
[Optimizing deep learning on P3dn with EFA](https://aws.amazon.com/blogs/compute/optimizing-deep-learning-on-p3-and-p3dn-with-efa/).

When NCCL is not observable, give the operator this to collect on one affected job:
`NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NET,P2P,SHM,NVLS NCCL_DEBUG_FILE=/fsx/nccl_%h_%p.log`
(subsystem names from the NCCL logging page), then search the files for the lines above.

## 2. NVLink and NVSwitch fabric

The CloudWatch agent's NVIDIA plugin does **not** collect any NVLink counter (its full
metric list is utilization, temperature, power, memory, PCIe link, encoder, and clocks).
NVLink health reaches AWS only through the system log:

| Signal | Where | Meaning |
|--------|-------|---------|
| `NVRM: Xid ...: 74` | Kernel log, HyperPod HMA | NVLink error (NVIDIA catalog: immediate action per NVLink workflow, investigatory action contact support). Hardware class |
| `NVRM: Xid ...: 71`, `NVLink: fatal error detected on link <n>` | Kernel log, HyperPod HMA (`reason: XidHardwareFailure`) | Fatal NVLink error; example in the HyperPod HMA documentation. Hardware class |
| `NVRM: Xid ...: 155` / `156` | Kernel log | GPU NVLink flit CRC error / lane error (listed by Amazon ECS GPU auto repair). Hardware class |
| Other `NVRM:` lines that mention NVLink without `Xid` | Kernel log | Driver diagnostics. List them in the timeline with node and hour. **Do not classify** them or call them a cause without corroboration |
| Fabric Manager start: `Started "Nvidia Fabric Manager"` | System log (`/var/log/messages` or journal) | Fabric Manager service started. Applies to NVSwitch instance types (section 4) |
| `CX Bridge device ... is usable for NVLink subnet management` | System log | P6-B200 and P6-B300 only: AWS documents that on these types Fabric Manager configures NVFabric through ConnectX bridge devices, so this line shows the bridge was found |
| Fabric Manager absent, failed, or restarting on an NVSwitch instance | System log | NVLink between GPUs may not be up. Hardware or driver-stack problem: node verdict `REBOOT`, then `REPLACE` if it recurs. AWS documents Fabric Manager as required on P6-B200 and P6-B300; on other NVSwitch types, report a failure as a strong signal but label the NVLink impact `Hypothesis (to validate)` with `nvidia-smi topo -m` as the check |
| `nvidia-fabricmanager.service: ... PIDFile= references a path below legacy directory /var/run/` | System log | systemd path warning. **Benign.** Exclude it before counting Fabric Manager "errors" |

Sources: [NVIDIA Xid catalog](https://docs.nvidia.com/deploy/xid-errors/analyzing-xid-catalog.html),
[HyperPod health monitoring](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-eks-resiliency-health-monitoring-agent.html),
[ECS GPU auto repair Xid list](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/managed-instances-gpu-auto-repair.html),
[EC2 public NVIDIA drivers, P6-B200 and P6-B300 considerations](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/public-nvidia-driver.html),
[CloudWatch agent NVIDIA metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Agent-NVIDIA-GPU.html).

On-node confirmation for the operator (not available through AWS APIs): NVLink status and
error counters from `nvidia-smi nvlink` and DCGM, and `systemctl status nvidia-fabricmanager`.

### On-node NVLink and fabric fields, captured from a live p6-b300.48xlarge

Driver 595.91.07, CUDA 13.2, 8 x `NVIDIA B300 SXM6 AC`. Quote these field names exactly.
They are the operator-side evidence for the NVLink 5 Xid family 144 to 150
(`references/xid-triage.md` rule 10), which is Blackwell only.

| Command | Healthy reading observed | How to read it |
|---------|--------------------------|----------------|
| `nvidia-smi nvlink -s` | `Link <n>: 53.125 GB/s` for every link | A link that is missing, or reads `<inactive>`, is down. Compare the link count across all 8 GPUs; an asymmetry is the fault location |
| `nvidia-smi nvlink -e` | All zero: `Malformed packet Errors`, `Buffer overrun Errors`, `Rx Errors`, `Rx remote Errors`, `Rx General Errors`, `Local link integrity Errors`, `Tx discards`, `Link recovery successful events`, `Link recovery failed events`, `Total link recovery events`, `Effective Errors`, `Symbol Errors` | These are the exact counter names on driver 595.91.07. Non-zero on one link on one GPU points at that link, and these are the counters to quote when an Xid 144 to 150 names a link. `Link recovery failed events` above zero is the strongest of them. Note the older `Replay Errors` / `Recovery Errors` / `CRC Errors` names are **not** present on this driver, so do not look for them |
| `nvidia-smi nvlink -e`, FEC fields | `FEC Errors - 0: <large and growing>`, buckets 1 to 15 at or near `0` | **Do not report bucket 0 as an error count.** It is the corrected-codeword counter and reads in the billions on a healthy link (36,140,749,276 observed at boot). Only buckets climbing above 0 indicate real link stress |
| `nvidia-smi nvlink -e`, BER fields | `Effective BER: 15e-255`, `Symbol BER: 15e-255` | `15e-255` is the floating-point floor, meaning effectively zero. Do not read it as a large exponent or a high error rate |
| `nvidia-smi nvlink -e`, raw lane fields | `Raw BER Lane 0: 2061`, `Raw BER Lane 1: 1038`, `Raw BER Total: 1037`, `Raw Errors Lane 0: 82`, `Raw Errors Lane 1: 4` | **All of these were non-zero on a healthy node at boot.** They are pre-correction physical-layer counters, so a non-zero value is normal and is not a fault. Never report `Raw Errors` or `Raw BER` as evidence of an NVLink problem on its own. Use them only as a trend against the same link's earlier reading, and lead with the corrected counters above |
| `nvidia-smi -q`, `Fabric` section | `State: Completed`, `Status: Success`, `CliqueId: 0`, plus a per-GPU `GPU Fabric GUID` | `State` other than `Completed` or `Status` other than `Success` means the GPU has not joined the NVLink fabric. This is the single clearest fabric health field, better than parsing Fabric Manager log lines |
| `systemctl is-active nvidia-fabricmanager` | `active` | Anything else on an NVSwitch type is a REBOOT candidate per the table above |
| `nvidia-smi topo -m` | `NV18` between every GPU pair | `NV18` means 18 bonded NVLinks. A pair reading `SYS` or `PHB` instead has lost NVLink and fell back to PCIe or the host interconnect, which is the topology-level version of the SHM fallback in section 1 |

Two cautions, both from this capture. The FEC bucket-0 counter and the `15e-255` BER floor
each look alarming and are not: a report that calls either one an error is wrong. And
`dmesg` on a healthy node carries benign `NVRM: API mismatch` warnings when a userspace
component (for example `nvidia-gridd`) lags the kernel module version. Exclude those
before counting NVRM errors, the same way the Fabric Manager `PIDFile=` warning is excluded.

## 3. EFA error counters

| Source | Metric names |
|--------|--------------|
| CloudWatch agent `efa` section (namespace `CWAgent`) | `efa_retrans_pkts`, `efa_retrans_timeout_events`, `efa_impaired_remote_conn_events`, `efa_unresponsive_remote_events`, `efa_rx_dropped`, `efa_rdma_read_wr_err`, `efa_rdma_write_wr_err` |
| HyperPod observability EFA exporter | `node_amazonefa_*` (for example `node_amazonefa_rx_drops`, `node_amazonefa_rdma_read_wr_err`) |
| On the node | `rdma -p statistic show`, or `/sys/class/infiniband/<device>/ports/<port>/hw_counters/` |

Read them as signals, not thresholds: a rise in retransmit timeouts, impaired or
unresponsive remote events, or work-request errors on the affected nodes, starting at or
before the hang, supports Branch D. A rise that starts after the hang is an effect.
Sources: [CloudWatch agent EFA metrics](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Agent-EFA.html),
[Monitor an EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa-working-monitor.html).

**Do not count `/sys/class/infiniband` entries to decide whether EFA is attached.** Verified
on a live `p6-b300.48xlarge` launched with no EFA interface at all: the OS still showed two
InfiniBand devices, `ibp198s0f0` and `ibp199s0f0`. They are **ConnectX bridge devices**
(driver `mlx5_ib` / `mlx5_core`, firmware `28.47.2526`), which is how Fabric Manager does
NVLink subnet management on P6-B200 and P6-B300, per the AWS public-driver page and the
`CX Bridge device ... is usable for NVLink subnet management` log line in section 2. They are
not network fabric. On the same node the `efa` kernel module was loaded with a zero
reference count and `/dev/infiniband` held only the ConnectX `uverbs`/`umad` pairs, while
`DescribeInstances` reported no interface with `InterfaceType` `efa` or `efa-only`.

So on a Blackwell node, an InfiniBand device count is evidence about the NVLink bridge, not
about EFA, and reading it as "2 EFA devices present" is a false positive. Count EFA the way
rule R2 says, from `DescribeInstances` `InterfaceType` `efa` or `efa-only` against
`MaximumEfaInterfaces`, and if you want on-node corroboration use `fi_info -p efa` (absent
on the base Deep Learning AMI unless libfabric is installed) or check the device driver
behind each InfiniBand entry rather than the entry itself.

## 4. Which instance types have an NVSwitch fabric

`DescribeInstanceTypes` does not report NVSwitch or NVLink. Use the "GPU Peer to Peer"
column of the [EC2 accelerated computing instance page](https://aws.amazon.com/ec2/instance-types/accelerated-computing/),
summarised here as checked:

| Instance types | GPU peer to peer | Treat as |
|----------------|------------------|----------|
| p4d.24xlarge, p4de.24xlarge | 600 GB/s NVSwitch | NVSwitch |
| p5.48xlarge, p5e.48xlarge, p5en.48xlarge | 900 GB/s NVSwitch | NVSwitch |
| p6-b200.48xlarge, p6-b300.48xlarge, P6e-GB200 UltraServers | 1800 GB/s NVSwitch | NVSwitch (P6e: NVLink domain spans the UltraServer) |
| p5.4xlarge and other single-GPU sizes | N/A | No intra-node GPU communication |
| Multi-GPU g7 and g7e sizes | Yes via PCIe | PCIe peer to peer, no NVSwitch |
| Multi-GPU g4dn, g5, g6, g6e sizes | Not listed | `NVSwitch presence unverified`; do not expect Fabric Manager; the operator checks `nvidia-smi topo -m` |

For a type not in this table, re-check the instance page. Never infer NVSwitch from the
GPU model name.

**The documented figure and `nvidia-smi` do not use the same units. Do not treat the
difference as a degraded fabric.** On a healthy `p6-b300.48xlarge`, `nvidia-smi topo -m`
reads `NV18` between every GPU pair (18 bonded NVLinks) and `nvidia-smi nvlink -s` reads
`53.125 GB/s` per link. That is 956.25 GB/s per direction, against the 1800 GB/s in the
table above, because the AWS and NVIDIA marketing figure is bidirectional while
`nvidia-smi` reports per-link unidirectional. An agent that divides the documented number
by the observed one will conclude the fabric is running at half width on a perfectly
healthy node. Compare link **count** and per-link **rate** across the GPUs in the node
instead, and treat an asymmetry between GPUs as the signal, never a mismatch against the
documented aggregate.

## 5. Software stack minimums

AWS publishes minimums for these types ([DLAMI P6 software requirements](https://docs.aws.amazon.com/dlami/latest/devguide/p6-support-dlami.html)):

| Component | P6-B200 | P6-B300 | P6e-GB200 |
|-----------|---------|---------|-----------|
| NVIDIA driver | R570 | R580 | R570 |
| NVLink 5 support | R570 | R580 | n/a in table |
| CUDA toolkit | 12.8 | 13.0 | 12.8 |
| Linux kernel | 6.1 | 6.1 | 6.12 |
| EFA installer | 1.41.0 | 1.44.0 | 1.42.0 |
| AWS OFI NCCL plugin | 1.15.0 | 1.17.1 | 1.15.0 |

For other GPU types no minimum table was found. Compare with the stack of a current DLAMI
that lists the type in `supported_ec2_instances` (DLAMI release notes) and report the
result as a comparison, not a pass or fail.

How to read versions without logging in:

| Component | Where |
|-----------|-------|
| NVIDIA driver | Kernel boot line `NVRM: loading NVIDIA UNIX Open Kernel Module for x86_64 <version>` in the shipped kernel log |
| Linux kernel | Kernel boot lines, if shipped |
| AWS OFI NCCL plugin | NCCL INFO lines at init, if shipped |
| CUDA toolkit, EFA installer | Not in AWS APIs; ask |

A version that cannot be read is `UNVERIFIED`, not a pass.
