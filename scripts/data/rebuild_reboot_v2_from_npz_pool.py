#!/usr/bin/env python
"""
Parallel rebuild of reboot-v2 HDF5 directly from rollout NPZ files.

Design:
- Each NPZ file is converted independently into one reboot-v2 shard HDF5
  using a multiprocessing pool.
- A final "single file" output is created as an HDF5 virtual dataset (VDS)
  that concatenates all shard datasets along sample dimension.

This avoids the slow intermediate consolidated rollout-HDF5 step.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import multiprocessing as mp
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import h5py
import numpy as np
from tqdm import tqdm


log = logging.getLogger("rebuild_reboot_v2_from_npz_pool")
_WORKER_CFG: Dict[str, object] = {}


@dataclass
class ShardMeta:
    source_name: str
    source_path: str
    shard_path: str
    num_samples: int


def _resolve_compression(name: str, level: int):
    if name == "none":
        return None, None
    if name == "lzf":
        return "lzf", None
    return "gzip", int(level)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parallel rebuild reboot-v2 HDF5 from rollout NPZ files."
    )
    parser.add_argument("--input_dir", type=str, default=None, help="Directory containing rollout NPZ files.")
    parser.add_argument("--input_glob", type=str, default=None, help="Glob for rollout NPZ files (supports absolute paths).")
    parser.add_argument("--output_hdf5", type=str, required=True, help="Final output reboot-v2 HDF5 path (VDS).")
    parser.add_argument("--shard_dir", type=str, required=True, help="Directory for per-file reboot-v2 shards.")
    parser.add_argument("--workers", type=int, default=96, help="Requested multiprocessing workers.")
    parser.add_argument("--task_chunksize", type=int, default=1, help="multiprocessing.imap_unordered chunksize.")
    parser.add_argument("--chunk_size", type=int, default=8192, help="Chunk size for shard HDF5 writes.")
    parser.add_argument("--cond_steps", type=int, default=3)
    parser.add_argument("--horizon_steps", type=int, default=16)
    parser.add_argument("--state_dim", type=int, default=64)
    parser.add_argument("--cmd_dim", type=int, default=58)
    parser.add_argument("--compression", type=str, default="gzip", choices=["gzip", "lzf", "none"])
    parser.add_argument("--compression_level", type=int, default=1)
    parser.add_argument("--state_key", type=str, default="state")
    parser.add_argument("--action_key", type=str, default="action")
    parser.add_argument("--overwrite", action="store_true", help="Delete existing shard dir and output file before running.")
    parser.add_argument("--max_files", type=int, default=None, help="Optional cap for debug runs.")
    parser.add_argument("--recovery_base_ang_thr", type=float, default=2.2)
    parser.add_argument("--recovery_joint_vel_thr", type=float, default=14.6)
    parser.add_argument("--recovery_base_lin_thr", type=float, default=0.9)
    return parser.parse_args()


def _list_npz_files(input_dir: str | None, input_glob: str | None, max_files: int | None) -> List[Path]:
    files: List[Path] = []
    if input_glob:
        files.extend(Path(p).resolve() for p in glob.glob(input_glob))
    if input_dir:
        files.extend(p.resolve() for p in Path(input_dir).expanduser().resolve().glob("*.npz"))
    unique = sorted({p for p in files if p.is_file()})
    if max_files is not None and max_files > 0:
        unique = unique[: max_files]
    return unique


def _init_worker(cfg_json: str) -> None:
    global _WORKER_CFG
    _WORKER_CFG = json.loads(cfg_json)
    # Keep numpy kernels predictable per process.
    # The process-level pool is the intended parallelism.
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        value = _WORKER_CFG.get(key)
        if value is None:
            continue
        os.environ[key] = str(value)


def _worker_build_shard(npz_path_str: str) -> Tuple[str, str, str, int]:
    cfg = _WORKER_CFG
    npz_path = Path(npz_path_str)
    shard_dir = Path(str(cfg["shard_dir"]))
    shard_path = shard_dir / f"{npz_path.stem}.reboot_v2.hdf5"

    state_key = str(cfg["state_key"])
    action_key = str(cfg["action_key"])
    chunk_size = int(cfg["chunk_size"])
    cond_steps = int(cfg["cond_steps"])
    horizon_steps = int(cfg["horizon_steps"])
    state_dim = int(cfg["state_dim"])
    cmd_dim = int(cfg["cmd_dim"])
    recovery_base_ang_thr = float(cfg["recovery_base_ang_thr"])
    recovery_joint_vel_thr = float(cfg["recovery_joint_vel_thr"])
    recovery_base_lin_thr = float(cfg["recovery_base_lin_thr"])
    comp, comp_opts = _resolve_compression(str(cfg["compression"]), int(cfg["compression_level"]))

    if shard_path.exists():
        shard_path.unlink()

    with np.load(npz_path, allow_pickle=False) as data:
        if state_key not in data:
            raise KeyError(f"Missing key '{state_key}' in {npz_path}")
        if action_key not in data:
            raise KeyError(f"Missing key '{action_key}' in {npz_path}")

        # NPZ member access inflates arrays; this is file-level parallelized.
        state_all = np.asarray(data[state_key], dtype=np.float32)
        action_all = np.asarray(data[action_key], dtype=np.float32)

    if state_all.ndim != 2:
        raise ValueError(f"state ndim must be 2, got {state_all.ndim} in {npz_path}")
    if action_all.ndim != 3:
        raise ValueError(f"action ndim must be 3, got {action_all.ndim} in {npz_path}")
    if state_all.shape[0] != action_all.shape[0]:
        raise ValueError(f"state/action sample mismatch in {npz_path}: {state_all.shape[0]} vs {action_all.shape[0]}")
    if state_all.shape[1] < state_dim:
        raise ValueError(f"state dim too small in {npz_path}: {state_all.shape[1]} < {state_dim}")
    if state_all.shape[1] < cmd_dim:
        raise ValueError(f"state dim too small for cmd_dim in {npz_path}: {state_all.shape[1]} < {cmd_dim}")
    if action_all.shape[1] < horizon_steps:
        raise ValueError(f"action horizon too small in {npz_path}: {action_all.shape[1]} < {horizon_steps}")
    if action_all.shape[2] < 29:
        raise ValueError(f"action dim too small in {npz_path}: {action_all.shape[2]} < 29")

    n = int(state_all.shape[0])
    if n <= 0:
        raise ValueError(f"No samples in {npz_path}")

    chunk = max(1, min(chunk_size, n))
    hist_offsets = np.arange(-(cond_steps - 1), 1, dtype=np.int64)
    delta_offsets = np.arange(1, horizon_steps + 1, dtype=np.int64)

    with h5py.File(shard_path, "w") as fout:
        fout.attrs["num_samples"] = n
        fout.attrs["source_file"] = str(npz_path)
        fout.attrs["schema"] = "reboot_v2_from_npz_shard"
        fout.attrs["cond_steps"] = cond_steps
        fout.attrs["horizon_steps"] = horizon_steps
        fout.attrs["state_dim"] = state_dim
        fout.attrs["cmd_dim"] = cmd_dim
        fout.attrs["recovery_base_ang_thr"] = recovery_base_ang_thr
        fout.attrs["recovery_joint_vel_thr"] = recovery_joint_vel_thr
        fout.attrs["recovery_base_lin_thr"] = recovery_base_lin_thr

        ds_action = fout.create_dataset(
            "action",
            shape=(n, horizon_steps, 29),
            dtype="f4",
            chunks=(chunk, horizon_steps, 29),
            compression=comp,
            compression_opts=comp_opts,
        )
        ds_state = fout.create_dataset(
            "state",
            shape=(n, state_dim),
            dtype="f4",
            chunks=(chunk, state_dim),
            compression=comp,
            compression_opts=comp_opts,
        )
        ds_cmd = fout.create_dataset(
            "cmd",
            shape=(n, cmd_dim),
            dtype="f4",
            chunks=(chunk, cmd_dim),
            compression=comp,
            compression_opts=comp_opts,
        )
        ds_state_hist = fout.create_dataset(
            "state_hist",
            shape=(n, cond_steps, state_dim),
            dtype="f4",
            chunks=(chunk, cond_steps, state_dim),
            compression=comp,
            compression_opts=comp_opts,
        )
        ds_cmd_hist = fout.create_dataset(
            "cmd_hist",
            shape=(n, cond_steps, cmd_dim),
            dtype="f4",
            chunks=(chunk, cond_steps, cmd_dim),
            compression=comp,
            compression_opts=comp_opts,
        )
        ds_state_delta = fout.create_dataset(
            "state_delta",
            shape=(n, horizon_steps, state_dim),
            dtype="f4",
            chunks=(chunk, horizon_steps, state_dim),
            compression=comp,
            compression_opts=comp_opts,
        )
        ds_recovery_flag = fout.create_dataset(
            "recovery_flag",
            shape=(n, 1),
            dtype="f4",
            chunks=(chunk, 1),
            compression=comp,
            compression_opts=comp_opts,
        )

        for a in range(0, n, chunk):
            b = min(a + chunk, n)
            idx = np.arange(a, b, dtype=np.int64)
            hist_idx = np.clip(idx[:, None] + hist_offsets[None, :], 0, n - 1)
            fut_idx = np.clip(idx[:, None] + delta_offsets[None, :], 0, n - 1)

            state_chunk = state_all[a:b, :state_dim]
            cmd_chunk = state_chunk[:, :cmd_dim]
            action_chunk = action_all[a:b, :horizon_steps, :29]

            state_hist_chunk = state_all[hist_idx, :state_dim]
            cmd_hist_chunk = state_all[hist_idx, :cmd_dim]
            state_delta_chunk = state_all[fut_idx, :state_dim] - state_chunk[:, None, :]

            joint_vel_norm = np.linalg.norm(state_chunk[:, 29:58], axis=1)
            base_lin_norm = np.linalg.norm(state_chunk[:, 58:61], axis=1)
            base_ang_norm = np.linalg.norm(state_chunk[:, 61:64], axis=1)
            recovery_chunk = (
                (base_ang_norm > recovery_base_ang_thr)
                | (joint_vel_norm > recovery_joint_vel_thr)
                | (base_lin_norm > recovery_base_lin_thr)
            ).astype(np.float32)[:, None]

            out_slice = slice(a, b)
            ds_action[out_slice] = action_chunk
            ds_state[out_slice] = state_chunk
            ds_cmd[out_slice] = cmd_chunk
            ds_state_hist[out_slice] = state_hist_chunk
            ds_cmd_hist[out_slice] = cmd_hist_chunk
            ds_state_delta[out_slice] = state_delta_chunk
            ds_recovery_flag[out_slice] = recovery_chunk

        fout.create_dataset("file_offsets", data=np.asarray([0], dtype=np.int64))
        fout.create_dataset("file_names", data=np.asarray([npz_path.name], dtype="S"))

    return npz_path.name, str(npz_path), str(shard_path), n


def _create_vds(
    metas: List[ShardMeta],
    output_hdf5: Path,
    cond_steps: int,
    horizon_steps: int,
    state_dim: int,
    cmd_dim: int,
) -> None:
    total = int(sum(m.num_samples for m in metas))
    if total <= 0:
        raise ValueError("No samples available to create VDS.")

    if output_hdf5.exists():
        output_hdf5.unlink()
    output_hdf5.parent.mkdir(parents=True, exist_ok=True)

    specs = {
        "action": ((horizon_steps, 29), np.float32),
        "state": ((state_dim,), np.float32),
        "cmd": ((cmd_dim,), np.float32),
        "state_hist": ((cond_steps, state_dim), np.float32),
        "cmd_hist": ((cond_steps, cmd_dim), np.float32),
        "state_delta": ((horizon_steps, state_dim), np.float32),
        "recovery_flag": ((1,), np.float32),
    }

    file_offsets = []
    file_names = []
    cursor = 0
    for m in metas:
        file_offsets.append(cursor)
        file_names.append(m.source_name)
        cursor += int(m.num_samples)

    with h5py.File(output_hdf5, "w", libver="latest") as fout:
        fout.attrs["num_samples"] = total
        fout.attrs["schema"] = "reboot_v2_vds_from_npz_shards"
        fout.attrs["cond_steps"] = int(cond_steps)
        fout.attrs["horizon_steps"] = int(horizon_steps)
        fout.attrs["state_dim"] = int(state_dim)
        fout.attrs["cmd_dim"] = int(cmd_dim)
        fout.attrs["num_shards"] = int(len(metas))
        fout.attrs["vds"] = True

        for name, (tail_shape, dtype) in specs.items():
            layout = h5py.VirtualLayout(shape=(total,) + tail_shape, dtype=dtype)
            start = 0
            for m in metas:
                n = int(m.num_samples)
                src = h5py.VirtualSource(m.shard_path, name, shape=(n,) + tail_shape)
                layout[start : start + n, ...] = src
                start += n
            fout.create_virtual_dataset(name, layout)

        fout.create_dataset("file_offsets", data=np.asarray(file_offsets, dtype=np.int64))
        fout.create_dataset("file_names", data=np.asarray(file_names, dtype="S"))
        fout.create_dataset("shard_paths", data=np.asarray([m.shard_path for m in metas], dtype="S"))


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    npz_files = _list_npz_files(args.input_dir, args.input_glob, args.max_files)
    if not npz_files:
        raise FileNotFoundError("No NPZ files found. Provide --input_dir or --input_glob.")

    shard_dir = Path(args.shard_dir).expanduser().resolve()
    output_hdf5 = Path(args.output_hdf5).expanduser().resolve()
    if args.overwrite:
        if shard_dir.exists():
            shutil.rmtree(shard_dir)
        if output_hdf5.exists():
            output_hdf5.unlink()
    shard_dir.mkdir(parents=True, exist_ok=True)

    requested_workers = max(1, int(args.workers))
    # File-level task parallelism cannot exceed file count.
    effective_workers = min(requested_workers, len(npz_files))
    log.info(
        "NPZ files=%d, requested_workers=%d, effective_workers=%d",
        len(npz_files),
        requested_workers,
        effective_workers,
    )

    cfg = {
        "shard_dir": str(shard_dir),
        "state_key": args.state_key,
        "action_key": args.action_key,
        "chunk_size": int(args.chunk_size),
        "cond_steps": int(args.cond_steps),
        "horizon_steps": int(args.horizon_steps),
        "state_dim": int(args.state_dim),
        "cmd_dim": int(args.cmd_dim),
        "compression": args.compression,
        "compression_level": int(args.compression_level),
        "recovery_base_ang_thr": float(args.recovery_base_ang_thr),
        "recovery_joint_vel_thr": float(args.recovery_joint_vel_thr),
        "recovery_base_lin_thr": float(args.recovery_base_lin_thr),
        # Ensure numpy libs do not oversubscribe threads per process.
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }

    ctx = mp.get_context("spawn")
    worker_input = [str(p) for p in npz_files]
    results: Dict[str, ShardMeta] = {}
    with ctx.Pool(
        processes=effective_workers,
        initializer=_init_worker,
        initargs=(json.dumps(cfg),),
        maxtasksperchild=1,
    ) as pool:
        it = pool.imap_unordered(
            _worker_build_shard,
            worker_input,
            chunksize=max(1, int(args.task_chunksize)),
        )
        for source_name, source_path, shard_path, num_samples in tqdm(
            it, total=len(worker_input), desc="Building shards"
        ):
            results[source_name] = ShardMeta(
                source_name=source_name,
                source_path=source_path,
                shard_path=shard_path,
                num_samples=int(num_samples),
            )

    ordered_metas = [results[p.name] for p in npz_files]
    total_samples = int(sum(m.num_samples for m in ordered_metas))
    log.info("Built %d shard files, total_samples=%d", len(ordered_metas), total_samples)

    _create_vds(
        metas=ordered_metas,
        output_hdf5=output_hdf5,
        cond_steps=int(args.cond_steps),
        horizon_steps=int(args.horizon_steps),
        state_dim=int(args.state_dim),
        cmd_dim=int(args.cmd_dim),
    )

    with h5py.File(output_hdf5, "r") as f:
        keys = sorted(list(f.keys()))
        log.info("Final VDS written: %s", output_hdf5)
        log.info("Final keys: %s", keys)
        log.info("Final num_samples=%d", int(f.attrs.get("num_samples", -1)))
        log.info("action shape=%s", str(f["action"].shape))
        log.info("state shape=%s", str(f["state"].shape))


if __name__ == "__main__":
    main()
