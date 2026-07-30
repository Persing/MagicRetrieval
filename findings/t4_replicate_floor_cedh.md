# T4 — same-seed replicate floor

> **Standing confound — recorded, not solved.**
> Every co-occurrence-derived ground truth here inherits EDHREC circularity: the deck data is not independent of EDHREC's recommendations.
> The effect is strongest in the Archidekt casual corpus.
> All arms inherit it equally, so relative comparisons between arms survive; absolute numbers do not mean much.
> Recorded, not solved.

Arm **A** trained twice at seed 42 on **cedh**, everything else held
identical. The gap between the two runs is the variance the seed does *not* control: cuDNN/cuBLAS
reduction-order nondeterminism on the GPU.

| scorer | run 1 | run 2 | delta |
|---|---|---|---|
| centroid | 0.0098 | 0.0098 | 0.0000 |
| max_sim | 0.1282 | 0.1282 | 0.0000 |
| popularity | 0.6912 | 0.6912 | 0.0000 |
| random | 0.0013 | 0.0013 | 0.0000 |

`torch.use_deterministic_algorithms(True)` is deliberately not set: honest seed variance is the
point of running five seeds, and forcing determinism would suppress it as well as costing speed.
So the floor is measured rather than removed.

**How to read the null rule against this.** The frozen rule calls a gap smaller than the pooled
seed sd a null. This number is the floor beneath that: an arm difference near
0.0000 on recall_at_50 is kernel noise regardless of what the seed sd says.
