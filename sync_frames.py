# This script matches all frames by their timestamps (coming from the ntp system clock of the nucs).
# This avoids frame mismatching. You could simply only count each frame upwards for every nuc.
# But there is the possiblity that one Nuc might start faster and already processing a pulse from the trigger.
# Then there is a mismatch in order, therefore this script.

# All cameras are guaranteed to start exposure at the same time (through hardware trigger).
# All frames are asigned with ~same timestamp, there can be a drift (not perfect ntp clock).
# This script groups all frames that have a timestamp in a given range.

# This script is partly written with the help of AI.

import os
import argparse
import shutil
import numpy as np
import pandas as pd
from tqdm import tqdm


TOLERANCE = 0.3  # max. time difference within a group, as fraction of the period (time between two trigger pulses)


def load_camera(cam_dir):
    '''
    Reads frames.csv of one camera folder (e.g. recordings/test/nucN).

    :param cam_dir: folder of one camera
    :return: timestamps in ns (np.array) and paths to the images (list)
    '''
    csv_path = os.path.join(cam_dir, "frames.csv")
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"{csv_path} not found.")

    df = pd.read_csv(csv_path)
    if len(df) < 2:
        raise Exception(f"{csv_path} contains less than 2 frames.")

    times = df["host_time_ns"].to_numpy(dtype=np.int64)
    files = [os.path.join(cam_dir, f) for f in df["file"]]

    return times, files


def estimate_offset(times, times_ref, period):
    '''
    Estimates the constant offset of one camera to the reference camera (clock error + latency).
    For every frame the nearest frame of the reference camera is searched.
    The median of all time differences is the offset.

    :param times: timestamps of the camera
    :param times_ref: timestamps of the reference camera
    :param period: time between two trigger pulses in ns
    :return: offset in ns
    '''
    diffs = []
    for t in times:
        # nearest frame of the reference camera
        idx = np.argmin(np.abs(times_ref - t))
        d = t - times_ref[idx]
        # only use it if it is closer than half a period (otherwise it probably belongs to another pulse)
        if abs(d) < period / 2:
            diffs.append(d)

    if len(diffs) == 0:
        print("Warning: no matching frames found for the offset. Offset is set to 0.")
        return 0

    return int(np.median(diffs))


def sync(recording):
    '''
    Groups the frames of all cameras by time. Only groups that contain a frame of every camera are kept.
    Makes folder "synced" with one folder per camera. Same image name = same trigger pulse.
    Makes "synced/groups.csv" where every line contains: index, time_ns, original image name of every camera

    :param recording: folder of the recording (e.g. recordings/test)
    '''
    print('------------------ SYNC FRAMES -------------------')

    # every folder with a frames.csv is one camera (nucN, nucNW, ...)
    cam_names = []
    for name in sorted(os.listdir(recording)):
        if os.path.exists(os.path.join(recording, name, "frames.csv")):
            cam_names.append(name)

    if len(cam_names) == 0:
        raise Exception(f"No camera folder with frames.csv found in {recording}.")

    # load timestamps and image paths of all cameras
    times = []
    files = []
    for name in cam_names:
        t, f = load_camera(os.path.join(recording, name))
        times.append(t)
        files.append(f)
        print(f"{name}: {len(t)} frames")

    # time between two trigger pulses (median over all cameras)
    periods = []
    for t in times:
        periods.append(np.median(np.diff(t)))
    period = np.median(periods)
    print(f"Trigger frequency: {1e9 / period:.1f} Hz")

    # camera with the most frames is the reference
    num_frames = [len(t) for t in times]
    ref = int(np.argmax(num_frames))
    print(f"Reference camera: {cam_names[ref]}")

    # compute all offsets first, then subtract them
    offsets = []
    for c in range(len(cam_names)):
        offset = estimate_offset(times[c], times[ref], period)
        offsets.append(offset)
        print(f"Offset {cam_names[c]}: {offset / 1e6:+.1f} ms")

    for c in range(len(cam_names)):
        times[c] = times[c] - offsets[c]

    # put all frames of all cameras in one list (time, camera index, row in frames.csv) and sort by time
    all_frames = []
    for c in range(len(cam_names)):
        for row in range(len(times[c])):
            all_frames.append((times[c][row], c, row))
    all_frames.sort(key=lambda x: x[0])

    # frames that are close in time belong to the same trigger pulse --> same group
    groups = []
    for t, c, row in all_frames:
        if len(groups) == 0 or t - groups[-1]["t0"] > TOLERANCE * period:
            groups.append({"t0": t, "rows": {}})

        # if one camera has two frames in one group, only take the first one
        if c not in groups[-1]["rows"]:
            groups[-1]["rows"][c] = row

    # only keep groups with a frame of every camera
    complete = []
    for g in groups:
        if len(g["rows"]) == len(cam_names):
            complete.append(g)

    # stop if the output dir already exists, otherwise old and new frames get mixed
    out_dir = os.path.join(recording, "synced")
    if os.path.exists(out_dir):
        raise Exception(f"{out_dir} already exists. Delete it first.")
    for name in cam_names:
        os.makedirs(os.path.join(out_dir, name))

    # copy images, new name = group index
    group_rows = []
    for i, g in enumerate(tqdm(complete, desc="Copying synced frames")):
        line = [i, int(g["t0"])]
        for c, name in enumerate(cam_names):
            src = files[c][g["rows"][c]]
            dst = os.path.join(out_dir, name, f"{i:06d}.png")
            shutil.copy2(src, dst)
            line.append(os.path.basename(src))
        group_rows.append(line)

    df_groups = pd.DataFrame(group_rows, columns=["index", "time_ns"] + cam_names)
    groups_path = os.path.join(out_dir, "groups.csv")
    df_groups.to_csv(groups_path, index=False)

    print(f"{len(complete)} of {len(groups)} time points are complete (every camera has a frame).")
    print(f"Synced frames have been written to {out_dir}.\ngroups.csv has been written to {groups_path}")
    print('--------------------------------------------------')


def main():
    parser = argparse.ArgumentParser(description="Match frames of all cameras by timestamp")
    parser.add_argument("recording", type=str, nargs="?", help="e.g. recordings/test")
    args = parser.parse_args()

    # Interactive fallback
    if args.recording is None:
        args.recording = input("Recording folder (e.g. recordings/test): ").strip()

    if not os.path.exists(args.recording):
        raise FileNotFoundError(f"{args.recording} not found.")

    sync(args.recording)


# python3 sync_frames.py recordings/test

if __name__ == "__main__":
    main()