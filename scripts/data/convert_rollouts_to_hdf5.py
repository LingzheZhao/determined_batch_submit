"""
Convert BeyondMimic NPZ rollouts to a consolidated HDF5 dataset.

Stores traj/state (and optional keys if present in all files) into one HDF5 file
with chunking suitable for random access during training.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Dict, List, Optional

import h5py
import numpy as np
from tqdm import tqdm

log = logging.getLogger(__name__)


def _list_npz_files(input_dir: Optional[str], input_glob: Optional[str], input_files: Optional[List[str]]) -> List[Path]:
    files: List[Path] = []
    if input_files:
        files.extend([Path(f) for f in input_files])
    if input_glob:
        files.extend(Path().glob(input_glob))
    if input_dir:
        files.extend(sorted(Path(input_dir).glob("*.npz")))
    files = sorted({f.resolve() for f in files})
    return files


def _resolve_compression(compression: str, compression_level: int):
    if compression == "none":
        return None, None
    if compression == "gzip":
        return "gzip", compression_level
    if compression == "lzf":
        return "lzf", None
    raise ValueError(f"Unsupported compression: {compression}")


def main():
    parser = argparse.ArgumentParser(description="Convert NPZ rollouts to a single HDF5 file")
    parser.add_argument(
        "--input_dir",
        type=str,
        default=None,
        help="Directory containing NPZ files (optional if input_glob or input_files provided).",
    )
    parser.add_argument(
        "--input_glob",
        type=str,
        default=None,
        help="Glob pattern for NPZ files (e.g., /path/*.npz).",
    )
    parser.add_argument(
        "--input_files",
        type=str,
        nargs="*",
        default=None,
        help="Explicit list of NPZ files to include.",
    )
    parser.add_argument(
        "--output_file",
        type=str,
        required=True,
        help="Output HDF5 file path.",
    )
    parser.add_argument("--traj_key", type=str, default="traj")
    parser.add_argument("--state_key", type=str, default="state")
    parser.add_argument(
        "--extra_keys",
        type=str,
        default="action,cmd,env,z_style,state_hist,cmd_hist,state_delta,recovery_flag",
        help="Comma-separated list of optional keys to include if present in all files.",
    )
    parser.add_argument(
        "--require_extra_keys",
        action="store_true",
        help="Fail if any key from --extra_keys is missing or shape-inconsistent across files.",
    )
    parser.add_argument("--chunk_size", type=int, default=1024, help="Chunk size along sample dimension.")
    parser.add_argument(
        "--compression",
        type=str,
        default="none",
        choices=["none", "gzip", "lzf"],
        help="HDF5 compression to use.",
    )
    parser.add_argument("--compression_level", type=int, default=4, help="Gzip compression level.")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    npz_files = _list_npz_files(args.input_dir, args.input_glob, args.input_files)
    if not npz_files:
        raise FileNotFoundError("No NPZ files found. Provide --input_dir, --input_glob, or --input_files.")

    extra_keys = [k.strip() for k in args.extra_keys.split(",") if k.strip()]
    optional_present: Dict[str, bool] = {k: True for k in extra_keys}
    optional_shapes: Dict[str, tuple] = {}
    missing_by_key: Dict[str, List[str]] = {k: [] for k in extra_keys}
    inconsistent_by_key: Dict[str, List[str]] = {k: [] for k in extra_keys}

    total_samples = 0
    traj_shape = None
    state_shape = None
    file_offsets = []
    file_names = []

    log.info(f"Scanning {len(npz_files)} NPZ files...")
    for fp in tqdm(npz_files, desc="Scanning NPZs"):
        with np.load(fp, allow_pickle=False, mmap_mode="r") as data:
            if args.traj_key not in data or args.state_key not in data:
                raise KeyError(f"Missing {args.traj_key}/{args.state_key} in {fp}")

            traj = data[args.traj_key]
            state = data[args.state_key]

            if traj_shape is None:
                traj_shape = traj.shape[1:]
            elif traj.shape[1:] != traj_shape:
                raise ValueError(f"Inconsistent traj shape in {fp}: {traj.shape[1:]} vs {traj_shape}")

            if state_shape is None:
                state_shape = state.shape[1:]
            elif state.shape[1:] != state_shape:
                raise ValueError(f"Inconsistent state shape in {fp}: {state.shape[1:]} vs {state_shape}")

            for key in list(optional_present.keys()):
                if key in data:
                    shape = data[key].shape[1:]
                    if key not in optional_shapes:
                        optional_shapes[key] = shape
                    elif shape != optional_shapes[key]:
                        log.warning(
                            f"Skipping optional key '{key}' due to inconsistent shape in {fp}."
                        )
                        optional_present[key] = False
                        inconsistent_by_key[key].append(fp.name)
                else:
                    optional_present[key] = False
                    missing_by_key[key].append(fp.name)

            file_offsets.append(total_samples)
            file_names.append(fp.name)
            total_samples += traj.shape[0]

    if traj_shape is None or state_shape is None:
        raise ValueError("Failed to infer traj/state shapes.")

    kept_optional = [k for k, v in optional_present.items() if v]
    if args.require_extra_keys:
        failures = []
        for key in extra_keys:
            if not optional_present[key]:
                missing = missing_by_key.get(key, [])
                inconsistent = inconsistent_by_key.get(key, [])
                reason_parts = []
                if missing:
                    reason_parts.append(f"missing in {len(missing)} file(s), e.g. {missing[:3]}")
                if inconsistent:
                    reason_parts.append(
                        f"inconsistent shape in {len(inconsistent)} file(s), e.g. {inconsistent[:3]}"
                    )
                if not reason_parts:
                    reason_parts.append("not retained due to scan failure")
                failures.append(f"{key}: {'; '.join(reason_parts)}")
        if failures:
            raise ValueError(
                "--require_extra_keys enabled, but some requested keys are unavailable: "
                + " | ".join(failures)
            )
    log.info(f"Optional keys included: {kept_optional if kept_optional else 'none'}")

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    chunk_size = max(1, args.chunk_size)
    compression, compression_opts = _resolve_compression(args.compression, args.compression_level)

    log.info(f"Writing HDF5 to {output_path} (samples={total_samples})")
    with h5py.File(output_path, "w") as h5:
        h5.attrs["num_samples"] = total_samples
        h5.attrs["traj_shape"] = traj_shape
        h5.attrs["state_shape"] = state_shape
        h5.attrs["source_files"] = len(npz_files)

        traj_ds = h5.create_dataset(
            args.traj_key,
            shape=(total_samples,) + traj_shape,
            dtype="f4",
            chunks=(min(chunk_size, total_samples),) + traj_shape,
            compression=compression,
            compression_opts=compression_opts,
        )
        state_ds = h5.create_dataset(
            args.state_key,
            shape=(total_samples,) + state_shape,
            dtype="f4",
            chunks=(min(chunk_size, total_samples),) + state_shape,
            compression=compression,
            compression_opts=compression_opts,
        )

        optional_datasets = {}
        for key in kept_optional:
            shape = optional_shapes[key]
            optional_datasets[key] = h5.create_dataset(
                key,
                shape=(total_samples,) + shape,
                dtype="f4",
                chunks=(min(chunk_size, total_samples),) + shape,
                compression=compression,
                compression_opts=compression_opts,
            )

        h5.create_dataset("file_offsets", data=np.asarray(file_offsets, dtype=np.int64))
        h5.create_dataset("file_names", data=np.asarray(file_names, dtype="S"))

        offset = 0
        for fp in tqdm(npz_files, desc="Writing HDF5"):
            with np.load(fp, allow_pickle=False, mmap_mode="r") as data:
                traj = data[args.traj_key]
                state = data[args.state_key]
                num = traj.shape[0]

                for start in range(0, num, chunk_size):
                    end = min(start + chunk_size, num)
                    slice_out = slice(offset + start, offset + end)
                    traj_ds[slice_out] = np.asarray(traj[start:end], dtype=np.float32)
                    state_ds[slice_out] = np.asarray(state[start:end], dtype=np.float32)
                    for key, ds in optional_datasets.items():
                        ds[slice_out] = np.asarray(data[key][start:end], dtype=np.float32)

                offset += num

    log.info("HDF5 conversion complete.")


if __name__ == "__main__":
    main()
