#!/usr/bin/env python3
"""Toy workload that repeatedly multiplies matrices."""

from __future__ import annotations

import argparse
import random
import time
from typing import List


Matrix = List[List[float]]


def build_matrix(size: int, seed: int) -> Matrix:
    rng = random.Random(seed)
    return [[rng.random() for _ in range(size)] for _ in range(size)]


def matmul(left: Matrix, right: Matrix) -> Matrix:
    size = len(left)
    result = [[0.0] * size for _ in range(size)]
    for i in range(size):
        row = result[i]
        left_row = left[i]
        for k in range(size):
            left_val = left_row[k]
            right_row = right[k]
            for j in range(size):
                row[j] += left_val * right_row[j]
    return result


def checksum(matrix: Matrix) -> float:
    return sum(sum(row) for row in matrix)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a toy matrix multiply loop")
    parser.add_argument("--size", type=int, default=64, help="Matrix size (NxN)")
    parser.add_argument("--iterations", type=int, default=-1, help="Iterations to run (-1 for infinite)")
    parser.add_argument("--sleep", type=float, default=0.2, help="Sleep time between iterations (seconds)")
    parser.add_argument("--log-every", type=int, default=1, help="Log every N iterations")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    left = build_matrix(args.size, seed=0)
    right = build_matrix(args.size, seed=1)

    iteration = 0
    while args.iterations < 0 or iteration < args.iterations:
        start = time.time()
        product = matmul(left, right)
        value = checksum(product)
        duration = time.time() - start

        if args.log_every > 0 and iteration % args.log_every == 0:
            print(
                f"iter={iteration} size={args.size} checksum={value:.4f} elapsed={duration:.3f}s",
                flush=True,
            )

        iteration += 1
        if args.sleep > 0:
            time.sleep(args.sleep)


if __name__ == "__main__":
    main()
