#!/usr/bin/env python3

import re
import sys
import math
import wave
import argparse
from typing import List, Tuple

import numpy as np
from PIL import Image, ImageDraw


def parse_path_d(path_d: str) -> List[Tuple[float, float]]:
    """
    Parse a simple path string of the form:
      Mx1 y1 L x2 y2 L x3 y3 ... Z
    into a list of (x, y) tuples representing polygon corners.
    This ignores arcs, beziers, etc.
    """
    # Remove any trailing 'Z' or 'z' that closes the path
    path_d = path_d.strip().rstrip("Zz")

    # Extract all floating-point numbers
    coords_str = re.findall(r"[0-9]*\.?[0-9]+", path_d)
    coords = list(map(float, coords_str))

    points = []
    for i in range(0, len(coords), 2):
        x = coords[i]
        y = coords[i + 1]
        points.append((x, y))

    return points


def parse_svg(svg_path: str):
    """
    Reads an SVG file, returns:
      - width, height (from the viewBox if present, else default 1200x1200)
      - a list of polygons, where each polygon is a list of (x,y) points
    """
    with open(svg_path, "r", encoding="utf-8") as f:
        svg_content = f.read()

    # Detect viewBox="0 0 1200 1200"
    match_viewbox = re.search(
        r'viewBox\s*=\s*"[+-]?(\d+)\s+[+-]?(\d+)\s+(\d+)\s+(\d+)"', svg_content
    )
    if match_viewbox:
        min_x = int(match_viewbox.group(1))
        min_y = int(match_viewbox.group(2))
        width = int(match_viewbox.group(3))
        height = int(match_viewbox.group(4))
    else:
        # fallback
        width, height = 1200, 1200

    # Grab <path ... d="..."> data
    path_regex = re.compile(r'<path[^>]+d="([^"]+)"[^>]*>', re.IGNORECASE)
    path_data_list = path_regex.findall(svg_content)

    polygons = []
    for path_d in path_data_list:
        pts = parse_path_d(path_d)
        if len(pts) >= 3:
            polygons.append(pts)

    return width, height, polygons


def draw_outlines_png(
    width: int, height: int, polygons: List[List[Tuple[float, float]]], output_png: str
):
    """
    Create a PNG with black background and green outlines (no fill).
    """
    img = Image.new("RGB", (width, height), "black")
    draw = ImageDraw.Draw(img)

    for poly_points in polygons:
        draw.polygon(poly_points, outline="green")

    img.save(output_png)
    print(
        f"Saved outlines to {output_png} ({width}x{height}), {len(polygons)} polygons."
    )


def polygons_to_wave_cycle_all(
    polygons: List[List[Tuple[float, float]]], samples_per_cycle: int = 2048
) -> (np.ndarray, np.ndarray):
    """
    Generates a single wave cycle (left=x, right=y) that time-multiplexes
    all polygons. The cycle is subdivided so that each polygon is drawn
    in a consecutive fraction of the cycle. Then the wave is closed by
    connecting back to the start of the first polygon.

    - polygons: list of polygons, each a list of (x,y).
    - samples_per_cycle: number of samples in one cycle.

    Returns (wave_x, wave_y) as float32 arrays of length samples_per_cycle.
    """

    if not polygons:
        return (
            np.zeros(samples_per_cycle, dtype=np.float32),
            np.zeros(samples_per_cycle, dtype=np.float32),
        )

    all_x = []
    all_y = []
    for poly in polygons:
        for x, y in poly:
            all_x.append(x)
            all_y.append(y)
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)

    width = max_x - min_x
    height = max_y - min_y
    if width == 0:
        width = 1.0
    if height == 0:
        height = 1.0

    # scale so largest dimension fits [-0.9..+0.9]
    dim = max(width, height)
    scale_factor = 1.8 / dim

    # center shift (optional simpler approach):
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0

    # subdivide the cycle among the polygons
    wave_x = np.zeros(samples_per_cycle, dtype=np.float32)
    wave_y = np.zeros(samples_per_cycle, dtype=np.float32)

    num_polygons = len(polygons)
    # fraction of cycle for each polygon
    fraction_per_poly = 1.0 / num_polygons

    sample_index = 0

    for poly_idx, poly in enumerate(polygons):
        # how many samples we allocate for this polygon
        start_frac = poly_idx * fraction_per_poly
        end_frac = (poly_idx + 1) * fraction_per_poly
        start_samp = int(start_frac * samples_per_cycle)
        end_samp = int(end_frac * samples_per_cycle)
        length = max(1, end_samp - start_samp)

        # We'll gather edges for interpolation
        edges = []
        for i in range(len(poly)):
            p1 = poly[i]
            p2 = poly[(i + 1) % len(poly)]
            edges.append((p1, p2))
        # total edges
        n_edges = len(edges)

        # For each sample in [start_samp .. end_samp-1], figure out
        # fractional position along the polygon perimeter
        for s in range(start_samp, end_samp):
            local_frac = (s - start_samp) / length  # in [0..1)
            # multiply by number of edges
            seg_length = 1.0 / n_edges
            seg_index = int(local_frac // seg_length)
            edge_phase = (local_frac % seg_length) / seg_length

            if seg_index >= n_edges:
                seg_index = n_edges - 1

            p1, p2 = edges[seg_index]
            # normalize + scale + center shift
            x1 = (p1[0] - center_x) * scale_factor
            y1 = (p1[1] - center_y) * scale_factor
            x2 = (p2[0] - center_x) * scale_factor
            y2 = (p2[1] - center_y) * scale_factor

            # linear interpolation
            x_val = x1 + edge_phase * (x2 - x1)
            y_val = y1 + edge_phase * (y2 - y1)

            wave_x[s] = x_val
            wave_y[s] = y_val

    # close the shape by connecting end back to start
    # The end of the wave might not match the beginning, so let's do
    # a quick crossfade on the last ~5 samples to the first sample
    crossfade_len = min(5, samples_per_cycle // 10)
    if crossfade_len > 1:
        for i in range(crossfade_len):
            alpha = i / crossfade_len
            wave_x[samples_per_cycle - crossfade_len + i] = (
                wave_x[samples_per_cycle - crossfade_len + i] * (1 - alpha)
                + wave_x[0] * alpha
            )
            wave_y[samples_per_cycle - crossfade_len + i] = (
                wave_y[samples_per_cycle - crossfade_len + i] * (1 - alpha)
                + wave_y[0] * alpha
            )

    return (wave_x, wave_y)


def repeat_cycle_to_duration(
    x_cycle: np.ndarray,
    y_cycle: np.ndarray,
    sample_rate: int,
    repeats_per_second: float,
    total_duration: float,
) -> (np.ndarray, np.ndarray):
    """
    Repeats the wave cycle `repeats_per_second` times per second,
    for `total_duration` seconds. So each cycle is repeated that many times
    in 1 second, effectively controlling the "refresh rate" on the scope.

    Returns final wave_x, wave_y as float32 arrays.
    """
    # One cycle length
    cycle_len = len(x_cycle)
    # how many cycles do we put in 1 second
    cycles_per_sec = repeats_per_second
    # total samples in 1 second
    samples_per_sec = sample_rate
    # so each cycle must occupy samples_per_sec / cycles_per_sec samples
    samples_per_cycle = int(samples_per_sec / cycles_per_sec)

    # We need total_duration * sample_rate total samples
    total_samps = int(total_duration * sample_rate)

    # resample x_cycle, y_cycle to fill samples_per_cycle
    # (if cycle_len != samples_per_cycle, we can stretch or compress)
    # then repeat it enough times to reach total_samps
    # nearest or linear interpolation
    t_original = np.linspace(0, 1, cycle_len, endpoint=False)
    t_target = np.linspace(0, 1, samples_per_cycle, endpoint=False)

    x_resamp = np.interp(t_target, t_original, x_cycle)
    y_resamp = np.interp(t_target, t_original, y_cycle)

    # number of cycles total we need
    cycles_total = total_samps // samples_per_cycle
    leftover = total_samps % samples_per_cycle

    big_x = np.zeros(total_samps, dtype=np.float32)
    big_y = np.zeros(total_samps, dtype=np.float32)

    for c in range(cycles_total):
        start_idx = c * samples_per_cycle
        big_x[start_idx : start_idx + samples_per_cycle] = x_resamp
        big_y[start_idx : start_idx + samples_per_cycle] = y_resamp

    # If there's leftover, fill partial cycle
    if leftover > 0:
        partial_t = np.linspace(0, 1, leftover, endpoint=False)
        x_part = np.interp(partial_t, t_original, x_cycle)
        y_part = np.interp(partial_t, t_original, y_cycle)
        start_l = cycles_total * samples_per_cycle
        big_x[start_l : start_l + leftover] = x_part
        big_y[start_l : start_l + leftover] = y_part

    return (big_x, big_y)


def write_stereo_wav(
    filename: str, left: np.ndarray, right: np.ndarray, sample_rate: int = 44100
):
    """
    Write a stereo 16-bit WAV from two float32 numpy arrays.
    """
    length = min(len(left), len(right))
    left = left[:length]
    right = right[:length]

    # float32 -> int16
    left_int16 = (left * 32767.0).clip(-32767, 32767).astype(np.int16)
    right_int16 = (right * 32767.0).clip(-32767, 32767).astype(np.int16)

    with wave.open(filename, "wb") as wavf:
        wavf.setnchannels(2)
        wavf.setsampwidth(2)  # 16 bits
        wavf.setframerate(sample_rate)
        interleaved = np.column_stack((left_int16, right_int16)).ravel()
        wavf.writeframes(interleaved.tobytes())

    print(f"Saved stereo WAV to {filename} (length={length/sample_rate:.2f} sec).")


def main():
    parser = argparse.ArgumentParser(
        description="Parse an SVG of polygons, output an outline PNG, and generate a single wave cycle that draws ALL polygons at once on XY scope."
    )
    parser.add_argument("svgfile", help="Input SVG file")
    parser.add_argument("pngfile", help="Output PNG file", nargs="?", default="out.png")
    parser.add_argument("wavfile", help="Output WAV file", nargs="?", default="out.wav")
    parser.add_argument(
        "--sample_rate", type=int, default=44100, help="Audio sample rate"
    )
    parser.add_argument(
        "--cycle_samples",
        type=int,
        default=2048,
        help="Number of samples in the wave cycle (before repeating)",
    )
    parser.add_argument(
        "--repeats_per_second",
        type=float,
        default=30.0,
        help="How many times per second we refresh the shape (like a 30Hz scope draw).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="Total duration of the output wave in seconds.",
    )
    args = parser.parse_args()

    width, height, polygons = parse_svg(args.svgfile)
    draw_outlines_png(width, height, polygons, args.pngfile)
    wave_x_cycle, wave_y_cycle = polygons_to_wave_cycle_all(
        polygons, samples_per_cycle=args.cycle_samples
    )

    wave_x, wave_y = repeat_cycle_to_duration(
        wave_x_cycle,
        wave_y_cycle,
        sample_rate=args.sample_rate,
        repeats_per_second=args.repeats_per_second,
        total_duration=args.duration,
    )

    write_stereo_wav(args.wavfile, wave_x, wave_y, sample_rate=args.sample_rate)


if __name__ == "__main__":
    main()
