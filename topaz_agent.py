"""Run on the Windows PC with Topaz installed, outside Docker.

CLI mode requires a Gigapixel Pro license. In manual mode open input.png in
Gigapixel and export the upscaled result as upscaled.png in the same job folder.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from PIL import Image


def manual_instruction(folder: Path, request: dict, reason: str = ""):
    marker = folder / "manual-instructions.txt"
    if not marker.exists():
        source = folder / "input.png"
        output = folder / "upscaled.png"
        marker.write_text(
            f"{reason}\nOpen {source} in Topaz Gigapixel. Upscale at least {request['scale']}x "
            f"and export a PNG to {output}. Minimum pixels: "
            f"{request['target_width']} x {request['target_height']}.\n", encoding="utf-8")
        print(f"Topaz job {request['job_id']}: {source} -> {output}", flush=True)


def handle(folder: Path, mode: str, executable: str):
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    source = folder / "input.png"
    output = folder / "upscaled.png"
    if output.exists() or (folder / "error.txt").exists():
        return
    if mode == "manual" or (folder / "manual-instructions.txt").exists():
        manual_instruction(folder, request)
        return
    work = folder / "cli-output"
    work.mkdir(exist_ok=True)
    try:
        result = subprocess.run([executable, "-i", str(source), "-o", str(work),
                        "--scale", str(request["scale"]), "--image-format", "png"],
                       check=True, timeout=3600, capture_output=True, text=True)
        candidates = list(work.glob("*.png"))
        if len(candidates) != 1:
            raise RuntimeError(f"Expected one Topaz PNG, got {len(candidates)}")
        with Image.open(candidates[0]) as image:
            image.verify()
        temporary = folder / "upscaled.partial"
        shutil.copyfile(candidates[0], temporary)
        os.replace(temporary, output)
        print(f"Topaz job {request['job_id']} completed", flush=True)
    except Exception as exc:
        if mode == "auto":
            detail = str(exc)
            if isinstance(exc, subprocess.CalledProcessError):
                detail = (exc.stderr or exc.stdout or detail)[-500:]
            manual_instruction(folder, request, f"CLI unavailable: {detail}")
        else:
            (folder / "error.txt").write_text(str(exc), encoding="utf-8")


def upload_print(folder: Path, share: Path | None):
    """Copy a completed TIFF to the UNC share, or keep it in the local spool."""
    if (folder / "print_uploaded.ok").exists() or (folder / "print_error.txt").exists():
        return
    try:
        request = json.loads((folder / "print_request.json").read_text(encoding="utf-8"))
        job_id = str(uuid.UUID(request["job_id"]))
        if job_id != folder.name:
            raise ValueError("Print request ID does not match its folder")
        source = folder / "print.tif"
        if not source.is_file():
            return
        if share is None:
            destination = source.resolve()
            (folder / "print_uploaded.ok").write_text(str(destination), encoding="utf-8")
            print(f"Print file ready locally: {destination}", flush=True)
            return
        if not share.is_dir():
            raise OSError(f"Windows share is not accessible: {share}")
        temporary = share / f"{job_id}.partial"
        destination = share / f"{job_id}.tif"
        shutil.copyfile(source, temporary)
        if temporary.stat().st_size != source.stat().st_size:
            raise OSError("Copied TIFF size does not match source")
        os.replace(temporary, destination)
        (folder / "print_uploaded.ok").write_text(str(destination), encoding="utf-8")
        print(f"Print file copied: {destination}", flush=True)
    except Exception as exc:
        (folder / "print_error.txt").write_text(str(exc)[:500], encoding="utf-8")


def share_from_env(path: Path) -> Path | None:
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("PRINT_SHARE_PATH="):
            value = line.partition("=")[2].strip()
            if not value:
                return None
            if not value.startswith("\\\\"):
                raise ValueError("PRINT_SHARE_PATH must be a UNC path: \\\\server\\share")
            return Path(value)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spool", type=Path, required=True, help="Same host folder mounted at /spool in Docker")
    parser.add_argument("--mode", choices=("auto", "manual", "cli"), default="auto")
    parser.add_argument("--exe", default="gigapixel")
    parser.add_argument("--config", type=Path, default=Path(".env"), help="Worker .env containing PRINT_SHARE_PATH")
    args = parser.parse_args()
    share = share_from_env(args.config)
    while True:
        for request in args.spool.glob("*/request.json"):
            handle(request.parent, args.mode, args.exe)
        for request in args.spool.glob("*/print_request.json"):
            upload_print(request.parent, share)
        time.sleep(3)


if __name__ == "__main__":
    main()
