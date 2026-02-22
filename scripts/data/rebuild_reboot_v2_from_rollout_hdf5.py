#!/usr/bin/env python
"""
Rebuild reboot-v2 HDF5 fields from rollout-schema HDF5 (state64 + action).

Input schema (rollout HDF5):
- action: (N, H, 29)
- state:  (N, 64) = [joint_pos(29), joint_vel(29), base_lin_vel(3), base_ang_vel(3)]
- file_offsets/file_names (optional but recommended)

Output schema (reboot-v2 HDF5):
- action, state
- cmd:         state[:, :58]
- state_hist:  temporal history from adjacent samples in same source file
- cmd_hist:    temporal history from adjacent samples in same source file
- state_delta: state[t+k] - state[t], k=1..H, from adjacent samples in same source file
- recovery_flag: heuristic instability label from velocity magnitudes
"""

from __future__ import annotations

import argparse
from pathlib import Path

import h5py
import numpy as np
from tqdm import tqdm


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild reboot-v2 HDF5 from rollout-schema HDF5."
    )
    parser.add_argument("--input_hdf5", type=str, required=True)
    parser.add_argument("--output_hdf5", type=str, required=True)
    parser.add_argument("--max_samples", type=int, default=None)
    parser.add_argument("--chunk_size", type=int, default=8192)
    parser.add_argument("--cond_steps", type=int, default=3)
    parser.add_argument("--horizon_steps", type=int, default=16)
    parser.add_argument("--state_dim", type=int, default=64)
    parser.add_argument("--cmd_dim", type=int, default=58)
    parser.add_argument("--compression", type=str, default="gzip", choices=["gzip", "lzf", "none"])
    parser.add_argument("--compression_level", type=int, default=1)
    parser.add_argument("--recovery_base_ang_thr", type=float, default=2.2)
    parser.add_argument("--recovery_joint_vel_thr", type=float, default=14.6)
    parser.add_argument("--recovery_base_lin_thr", type=float, default=0.9)
    return parser.parse_args()


def _resolve_compression(name: str, level: int):
    if name == "none":
        return None, None
    if name == "lzf":
        return "lzf", None
    return "gzip", int(level)


def _build_file_ranges(
    file_offsets: np.ndarray,
    total_samples: int,
    capped_total: int,
) -> tuple[list[tuple[int, int, int]], np.ndarray]:
    starts = file_offsets.astype(np.int64).tolist()
    ends = starts[1:] + [int(total_samples)]

    selected: list[tuple[int, int, int]] = []
    selected_offsets = []
    out_cursor = 0
    for file_idx, (start, end) in enumerate(zip(starts, ends)):
        if start >= capped_total:
            break
        end = min(end, capped_total)
        if end <= start:
            continue
        selected.append((file_idx, start, end))
        selected_offsets.append(out_cursor)
        out_cursor += end - start

    return selected, np.asarray(selected_offsets, dtype=np.int64)


def main() -> None:
    args = _parse_args()

    input_path = Path(args.input_hdf5).expanduser().resolve()
    output_path = Path(args.output_hdf5).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise FileNotFoundError(f"Input HDF5 not found: {input_path}")

    comp, comp_opts = _resolve_compression(args.compression, args.compression_level)

    with h5py.File(input_path, "r") as fin:
        if "action" not in fin or "state" not in fin:
            raise KeyError("Input HDF5 must contain 'action' and 'state'.")
        if "file_offsets" not in fin or "file_names" not in fin:
            raise KeyError("Input HDF5 must contain 'file_offsets' and 'file_names'.")

        action_ds = fin["action"]
        state_ds = fin["state"]
        file_offsets = np.asarray(fin["file_offsets"][:], dtype=np.int64)
        file_names = np.asarray(fin["file_names"][:])

        total_src = int(action_ds.shape[0])
        if int(state_ds.shape[0]) != total_src:
            raise ValueError("action/state sample count mismatch.")

        capped_total = min(
            total_src,
            int(args.max_samples) if args.max_samples is not None and int(args.max_samples) > 0 else total_src,
        )
        selected_ranges, out_offsets = _build_file_ranges(
            file_offsets=file_offsets,
            total_samples=total_src,
            capped_total=capped_total,
        )
        if not selected_ranges:
            raise ValueError("No selected file ranges after applying max_samples.")

        total_out = int(sum(end - start for _, start, end in selected_ranges))
        selected_file_names = np.asarray([file_names[i] for i, _, _ in selected_ranges], dtype=file_names.dtype)

        if output_path.exists():
            output_path.unlink()

        with h5py.File(output_path, "w") as fout:
            fout.attrs["num_samples"] = total_out
            fout.attrs["source_samples"] = total_src
            fout.attrs["source_hdf5"] = str(input_path)
            fout.attrs["schema"] = "reboot_v2_from_rollout_hdf5_state64_action"
            fout.attrs["cond_steps"] = int(args.cond_steps)
            fout.attrs["horizon_steps"] = int(args.horizon_steps)
            fout.attrs["state_dim"] = int(args.state_dim)
            fout.attrs["cmd_dim"] = int(args.cmd_dim)
            fout.attrs["recovery_base_ang_thr"] = float(args.recovery_base_ang_thr)
            fout.attrs["recovery_joint_vel_thr"] = float(args.recovery_joint_vel_thr)
            fout.attrs["recovery_base_lin_thr"] = float(args.recovery_base_lin_thr)

            chunk = max(1, min(int(args.chunk_size), total_out))
            h, sd, cd, cs = int(args.horizon_steps), int(args.state_dim), int(args.cmd_dim), int(args.cond_steps)

            ds_action = fout.create_dataset(
                "action",
                shape=(total_out, h, 29),
                dtype="f4",
                chunks=(chunk, h, 29),
                compression=comp,
                compression_opts=comp_opts,
            )
            ds_state = fout.create_dataset(
                "state",
                shape=(total_out, sd),
                dtype="f4",
                chunks=(chunk, sd),
                compression=comp,
                compression_opts=comp_opts,
            )
            ds_cmd = fout.create_dataset(
                "cmd",
                shape=(total_out, cd),
                dtype="f4",
                chunks=(chunk, cd),
                compression=comp,
                compression_opts=comp_opts,
            )
            ds_state_hist = fout.create_dataset(
                "state_hist",
                shape=(total_out, cs, sd),
                dtype="f4",
                chunks=(chunk, cs, sd),
                compression=comp,
                compression_opts=comp_opts,
            )
            ds_cmd_hist = fout.create_dataset(
                "cmd_hist",
                shape=(total_out, cs, cd),
                dtype="f4",
                chunks=(chunk, cs, cd),
                compression=comp,
                compression_opts=comp_opts,
            )
            ds_state_delta = fout.create_dataset(
                "state_delta",
                shape=(total_out, h, sd),
                dtype="f4",
                chunks=(chunk, h, sd),
                compression=comp,
                compression_opts=comp_opts,
            )
            ds_recovery = fout.create_dataset(
                "recovery_flag",
                shape=(total_out, 1),
                dtype="f4",
                chunks=(chunk, 1),
                compression=comp,
                compression_opts=comp_opts,
            )

            out_cursor = 0
            hist_offsets = np.arange(-(cs - 1), 1, dtype=np.int64)
            delta_offsets = np.arange(1, h + 1, dtype=np.int64)

            for (file_idx, start, end), file_out_offset in tqdm(
                list(zip(selected_ranges, out_offsets)),
                desc="Rebuilding files",
            ):
                _ = file_out_offset
                num = int(end - start)

                state_file = np.asarray(state_ds[start:end, :sd], dtype=np.float32)
                cmd_file = np.asarray(state_file[:, :cd], dtype=np.float32, order="C")

                joint_vel_norm = np.linalg.norm(state_file[:, 29:58], axis=1)
                base_lin_norm = np.linalg.norm(state_file[:, 58:61], axis=1)
                base_ang_norm = np.linalg.norm(state_file[:, 61:64], axis=1)
                recovery_file = (
                    (base_ang_norm > float(args.recovery_base_ang_thr))
                    | (joint_vel_norm > float(args.recovery_joint_vel_thr))
                    | (base_lin_norm > float(args.recovery_base_lin_thr))
                ).astype(np.float32)

                for a in range(0, num, int(args.chunk_size)):
                    b = min(a + int(args.chunk_size), num)
                    local_idx = np.arange(a, b, dtype=np.int64)

                    hist_idx = np.clip(local_idx[:, None] + hist_offsets[None, :], 0, num - 1)
                    fut_idx = np.clip(local_idx[:, None] + delta_offsets[None, :], 0, num - 1)

                    state_chunk = state_file[a:b]
                    cmd_chunk = cmd_file[a:b]
                    action_chunk = np.asarray(action_ds[start + a : start + b], dtype=np.float32)

                    state_hist_chunk = state_file[hist_idx]
                    cmd_hist_chunk = cmd_file[hist_idx]
                    state_delta_chunk = state_file[fut_idx] - state_chunk[:, None, :]
                    recovery_chunk = recovery_file[a:b, None]

                    out_slice = slice(out_cursor + a, out_cursor + b)
                    ds_action[out_slice] = action_chunk
                    ds_state[out_slice] = state_chunk
                    ds_cmd[out_slice] = cmd_chunk
                    ds_state_hist[out_slice] = state_hist_chunk
                    ds_cmd_hist[out_slice] = cmd_hist_chunk
                    ds_state_delta[out_slice] = state_delta_chunk
                    ds_recovery[out_slice] = recovery_chunk

                out_cursor += num

            fout.create_dataset("file_offsets", data=out_offsets)
            fout.create_dataset("file_names", data=selected_file_names)

    print(f"[OK] wrote: {output_path}")
    print(f"[INFO] samples={total_out}, source_total={total_src}")


if __name__ == "__main__":
    main()

