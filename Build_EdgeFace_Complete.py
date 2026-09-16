#!/usr/bin/env python3
"""Build the EdgeFace Android app from local models.

Run this one file with Python 3.10 or newer on Windows.
Edit the paths below when moving the project. No model is trained or downloaded.
The first build may download Android tools and libraries. Read SDK terms before accepting.
Functions come first and main() is at the end. The Android source below is readable text.
"""
from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


# 1. Set the paths here. The original ZIP files are not changed.
PROJECT_DIR = Path(r"C:\Users\123wi\OneDrive\Desktop\duits uni\project edge ai")
AGE_MODEL_PATH = PROJECT_DIR / "age_results_20260915_044643.zip"
GENDER_MODEL_PATH = PROJECT_DIR / "gender_results_20260915_030921.zip"
EXPRESSION_MODEL_PATH = PROJECT_DIR / "expression_android_bundle_20260915_142717_617660.zip"

# A model path can also be its extracted folder or its *_android.tflite file.
# Keep labels.txt, model_settings.json and format_validation.csv with that model.
APK_PATH = PROJECT_DIR / "EdgeFace.apk"
BUILD_LOG_DIR = PROJECT_DIR / "EdgeFace_Build"
SOURCE_COPY_DIR = PROJECT_DIR / "EdgeFace_Source"
BUILD_CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "EdgeFaceBuilder"
PRIVATE_KEY_DIR = Path.home() / ".edgeface-signing"

# Leave these as None to find installed tools automatically.
JAVA_HOME_PATH: Path | None = None
ANDROID_SDK_PATH: Path | None = None

# Keep the versions that compiled in the previous build.
APP_ID = "com.wpretorius.edgeface"
BUILDER_VERSION = "1.3.0"
GRADLE_VERSION = "8.13"
WRAPPER_SHA256 = "81a82aaea5abcc8ff68b3dfcb58b3c3c429378efd98e7433460610fecd7ae45f"
SDK_BUILD_TOOLS = "35.0.0"
SDK_PLATFORM = "android-36"
COMMAND_TOOLS_FILE = "commandlinetools-win-15859902_latest.zip"
COMMAND_TOOLS_SHA256 = "90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a"
SDK_TERMS_URL = "https://developer.android.com/studio#terms-and-conditions"
LOG_FILE: Path | None = None


def say(message: str) -> None:
    """Show a short message and keep it in the build log."""
    print(message, flush=True)
    if LOG_FILE is not None:
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")


def file_hash(path: Path) -> str:
    """Give each file a fingerprint."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    """Replace a file only after the new copy has been written."""
    if path.is_file() and path.stat().st_size == len(content) and path.read_bytes() == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def save_json(path: Path, values: Any) -> None:
    atomic_write(path, (json.dumps(values, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def safe_zip_path(root: Path, name: str) -> Path:
    """Do not let a ZIP file write outside its own folder."""
    clean = name.replace("\\", "/")
    parts = PurePosixPath(clean).parts
    if (not parts or clean.startswith("/") or ".." in parts
            or any(":" in part or "\x00" in part for part in parts)):
        raise ValueError("Unsafe ZIP path: " + name)
    target = (root / Path(*parts)).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError("Unsafe ZIP destination: " + name)
    return target


def extract_zip(source: Path | io.BytesIO, destination: Path,
                max_bytes: int = 3 * 1024 ** 3) -> list[Path]:
    """Unpack checked files. Do not extract links or special files."""
    destination.mkdir(parents=True, exist_ok=True)
    written = []
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > 50000 or sum(item.file_size for item in entries) > max_bytes:
            raise ValueError("The ZIP is larger than expected.")
        seen = set()
        planned = []
        for item in entries:
            target = safe_zip_path(destination, item.filename)
            key = str(target).casefold()
            if key in seen:
                raise ValueError("Repeated ZIP path: " + item.filename)
            seen.add(key)
            kind = stat.S_IFMT(item.external_attr >> 16)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("Links and special ZIP files are not allowed.")
            if item.flag_bits & 1:
                raise ValueError("Encrypted ZIP files are not supported.")
            planned.append((item, target))
        for item, target in planned:
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".extracting")
            with archive.open(item) as src, temporary.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            if temporary.stat().st_size != item.file_size:
                raise ValueError("ZIP file is incomplete: " + item.filename)
            temporary.replace(target)
            written.append(target)
    return written


def https_request(url: str):
    """Use normal certificate checks. Never turn HTTPS checking off."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Downloads must use HTTPS.")

    class HttpsRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urllib.parse.urlsplit(newurl).scheme != "https":
                raise ValueError("An unsafe download redirect was refused.")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(
        HttpsRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, headers={"User-Agent": "EdgeFace-Builder/1.0"})
    return opener.open(request, timeout=60)


def read_online_json(url: str) -> Any:
    with https_request(url) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Download information was larger than expected.")
    return json.loads(data.decode("utf-8"))


def download_file(url: str, path: Path, expected_hash: str) -> Path:
    """Reuse a valid download. Check every new download before using it."""
    if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash):
        raise ValueError("The download has no valid SHA-256 fingerprint.")
    expected_hash = expected_hash.lower()
    if path.is_file() and file_hash(path) == expected_hash:
        say("Using checked download: " + path.name)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    last_error = None
    for attempt in range(1, 4):
        try:
            say(f"Downloading {path.name} (attempt {attempt}/3)")
            with https_request(url) as response, partial.open("wb") as stream:
                expected_length = int(response.headers.get("Content-Length", "0"))
                received = 0
                last_print = time.monotonic()
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    received += len(block)
                    if time.monotonic() - last_print > 5:
                        total = f" / {expected_length / 1024 ** 2:.0f} MB" if expected_length else " MB"
                        say(f"  {received / 1024 ** 2:.0f}{total}")
                        last_print = time.monotonic()
            if expected_length and received != expected_length:
                raise RuntimeError("The download stopped before the end.")
            if file_hash(partial) != expected_hash:
                raise RuntimeError("The downloaded file failed its fingerprint check.")
            partial.replace(path)
            say("Download verified: " + path.name)
            return path
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as error:
            last_error = error
            if partial.exists():
                partial.unlink()
            if attempt < 3:
                time.sleep(attempt * 2)
    raise RuntimeError(
        f"Could not download {path.name}. Check the Internet connection, then run this same script again. "
        f"Address: {url}. Details: {last_error}")


def run_command(arguments: list[str | Path], *, env: dict[str, str] | None = None,
                cwd: Path | None = None, interactive: bool = False) -> str:
    """Run one build step. Stop if it fails instead of claiming success."""
    args = [str(part) for part in arguments]
    say("\n> " + subprocess.list2cmdline(args))
    if interactive:
        # License prompts need the real console, not a hidden input pipe.
        result = subprocess.run(args, env=env, cwd=cwd, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"This step failed with code {result.returncode}. See the text above.")
        return ""
    process = subprocess.Popen(args, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    result_parts = []
    try:
        assert process.stdout is not None
        while True:
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            result_parts.append(text)
            sys.stdout.write(text)
            sys.stdout.flush()
            if LOG_FILE is not None:
                with LOG_FILE.open("a", encoding="utf-8") as log:
                    log.write(text)
        code = process.wait()
    except BaseException:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
    text = "".join(result_parts)
    if code:
        raise RuntimeError(f"This step failed with code {code}. The full details are in {LOG_FILE}.")
    return text


def java_version(home: Path) -> int | None:
    """Only use a full Java kit, not a runtime without keytool."""
    suffix = ".exe" if os.name == "nt" else ""
    if not all((home / "bin" / (name + suffix)).is_file()
               for name in ("java", "javac", "keytool")):
        return None
    try:
        result = subprocess.run([str(home / "bin" / ("java" + suffix)), "-version"],
                                capture_output=True, text=True, errors="replace", timeout=20)
        match = re.search(r'version\s+"(\d+)', result.stderr + result.stdout)
        return int(match.group(1)) if result.returncode == 0 and match else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def find_java_candidates(cache: Path, explicit: str | None) -> list[Path]:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    for variable in ("JAVA_HOME", "JDK_HOME", "STUDIO_JDK"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]))
    for variable in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        if os.environ.get(variable):
            base = Path(os.environ[variable])
            candidates.extend([base / "Android" / "Android Studio" / "jbr",
                               base / "Programs" / "Android Studio" / "jbr"])
            for folder in (base / "Eclipse Adoptium", base / "Java", base / "Microsoft"):
                if folder.is_dir():
                    candidates.extend(p for p in folder.glob("*21*") if p.is_dir())
    candidates.extend(p.parent for p in (cache / "java").glob("*/bin") if p.is_dir())
    existing = shutil.which("java")
    if existing:
        candidates.append(Path(existing).resolve().parent.parent)
    return list(dict.fromkeys(p.expanduser().resolve() for p in candidates))


def prepare_java(cache: Path, explicit: str | None = None) -> Path:
    """Use Android Studio's Java 21, or download a private Java 21 copy."""
    for candidate in find_java_candidates(cache, explicit):
        if java_version(candidate) == 21:
            say("Using Java 21: " + str(candidate))
            return candidate
    if explicit:
        raise RuntimeError("--java-home must point to a complete Java 21 JDK folder.")
    say("Java 21 was not found. Getting a private Temurin JDK; no system settings are changed.")
    url = ("https://api.adoptium.net/v3/assets/latest/21/hotspot"
           "?architecture=x64&image_type=jdk&os=windows&vendor=eclipse")
    releases = read_online_json(url)
    if not isinstance(releases, list) or not releases:
        raise RuntimeError("The official Java download service returned no Java 21 build.")
    package = releases[0]["binary"]["package"]
    name = Path(package["name"]).name
    if not name.endswith(".zip"):
        raise RuntimeError("The Java download was not a Windows ZIP.")
    archive = download_file(package["link"], cache / "downloads" / name, package["checksum"])
    stage = Path(tempfile.mkdtemp(prefix="java_", dir=cache))
    try:
        extract_zip(archive, stage)
        homes = [p.parent.parent for p in stage.glob("*/bin/java.exe")]
        if len(homes) != 1 or java_version(homes[0]) != 21:
            raise RuntimeError("The downloaded Java 21 kit did not pass its launch check.")
        parent = cache / "java"
        parent.mkdir(exist_ok=True)
        target = parent / homes[0].name
        if target.exists():
            target = parent / (homes[0].name + "_" + str(int(time.time())))
        shutil.move(str(homes[0]), target)
        save_json(parent / "download_record.json", {"source": url, "package": package})
        return target
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def make_environment(java: Path, cache: Path, sdk: Path | None = None) -> dict[str, str]:
    """Set paths for this build only. Do not change the user's Python setup."""
    env = os.environ.copy()
    env["JAVA_HOME"] = str(java)
    env["PATH"] = str(java / "bin") + os.pathsep + env.get("PATH", "")
    env["GRADLE_USER_HOME"] = str(cache / "gradle-cache")
    env["JAVA_TOOL_OPTIONS"] = "-Dfile.encoding=UTF-8"
    # These can otherwise point to a different JDK or inject unrelated build flags.
    env.pop("_JAVA_OPTIONS", None)
    env.pop("JDK_JAVA_OPTIONS", None)
    if sdk is not None:
        env["ANDROID_HOME"] = str(sdk)
        env["ANDROID_SDK_ROOT"] = str(sdk)
    return env


def ask_for_sdk_download(cache: Path) -> None:
    """Ask before downloading Google's SDK. Never silently accept its terms."""
    acceptance = cache / "sdk_download_consent.json"
    if acceptance.is_file():
        previous = json.loads(acceptance.read_text(encoding="utf-8"))
        if previous.get("archive") == COMMAND_TOOLS_FILE and previous.get("accepted") is True:
            return
    say("\nSome Android tools are missing. Google's SDK license applies.")
    say("Read the terms here: " + SDK_TERMS_URL)
    say("The SDK manager can also show package licenses for you to accept.")
    answer = input("After reading the terms, type ACCEPT to download the SDK tools, or press Enter to stop: ").strip()
    if answer != "ACCEPT":
        raise RuntimeError("SDK download was not approved. Nothing was accepted on your behalf.")
    save_json(acceptance, {"accepted": True, "archive": COMMAND_TOOLS_FILE,
                          "terms": SDK_TERMS_URL, "date": datetime.now(timezone.utc).isoformat()})


def sdk_files_ready(sdk: Path) -> bool:
    return all((sdk / item).is_file() for item in [
        f"platforms/{SDK_PLATFORM}/android.jar",
        f"build-tools/{SDK_BUILD_TOOLS}/zipalign.exe",
        f"build-tools/{SDK_BUILD_TOOLS}/aapt2.exe",
        f"build-tools/{SDK_BUILD_TOOLS}/lib/apksigner.jar",
    ])


def sdk_command(java: Path, tool: Path, sdk: Path, *extra: str) -> list[str]:
    """Run the SDK manager directly through Java, so spaces in paths are safe."""
    jar = tool / "lib" / "sdkmanager-classpath.jar"
    if not jar.is_file():
        raise RuntimeError("The Android command-line tools are incomplete: " + str(jar))
    return [str(java / "bin" / "java.exe"), "-Dfile.encoding=UTF-8",
            "-Dcom.android.sdklib.toolsdir=" + str(tool), "-cp", str(jar),
            "com.android.sdklib.tool.sdkmanager.SdkManagerCli", "--sdk_root=" + str(sdk), *extra]


def prepare_sdk(java: Path, cache: Path, explicit: str | None) -> Path:
    """Reuse an installed SDK and install only the packages this app needs."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    else:
        for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]))
        if os.environ.get("LOCALAPPDATA"):
            candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk")
        candidates.append(cache / "android-sdk")
    for candidate in candidates:
        if sdk_files_ready(candidate):
            say("Using installed Android SDK: " + str(candidate))
            return candidate.resolve()
    sdk = (candidates[0] if explicit else next(
        (p for p in candidates if (p / "platforms").is_dir()), cache / "android-sdk")).resolve()
    sdk.mkdir(parents=True, exist_ok=True)
    # Prefer the most recently installed complete command-line tool directory.
    tools = sorted((sdk / "cmdline-tools").glob("*/lib/sdkmanager-classpath.jar"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if tools:
        tool = tools[0].parent.parent
    else:
        ask_for_sdk_download(cache)
        archive = download_file("https://dl.google.com/android/repository/" + COMMAND_TOOLS_FILE,
                                cache / "downloads" / COMMAND_TOOLS_FILE, COMMAND_TOOLS_SHA256)
        stage = Path(tempfile.mkdtemp(prefix="sdk_", dir=cache))
        try:
            extract_zip(archive, stage)
            original = stage / "cmdline-tools"
            if not (original / "lib/sdkmanager-classpath.jar").is_file():
                raise RuntimeError("The downloaded SDK tools did not have the expected files.")
            tool = sdk / "cmdline-tools" / "latest"
            tool.parent.mkdir(parents=True, exist_ok=True)
            if tool.exists():
                tool.rename(tool.with_name("previous_" + str(int(time.time()))))
            shutil.move(str(original), tool)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    env = make_environment(java, cache, sdk)
    say("\nRead the Android license text below. Type y only for licenses you accept.")
    run_command(sdk_command(java, tool, sdk, "--licenses"), env=env, interactive=True)
    run_command(sdk_command(java, tool, sdk, "--install", f"platforms;{SDK_PLATFORM}",
                            f"build-tools;{SDK_BUILD_TOOLS}"), env=env, interactive=True)
    if not sdk_files_ready(sdk):
        raise RuntimeError("The Android SDK installation is incomplete. See the SDK manager output.")
    say("Android SDK packages are ready.")
    return sdk


def prepare_wrapper(project: Path, cache: Path) -> None:
    """Get the small Gradle launcher and check its official fingerprint."""
    cached = download_file(
        "https://raw.githubusercontent.com/gradle/gradle/v8.13.0/gradle/wrapper/gradle-wrapper.jar",
        cache / "downloads" / "gradle-wrapper-8.13.jar", WRAPPER_SHA256)
    target = project / "gradle/wrapper/gradle-wrapper.jar"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cached, target)


def verify_models(project: Path) -> dict[str, Any]:
    """Make sure these are the approved models, not a different export."""
    folder = project / "app/src/main/assets/models"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    expected = {"age": ["Adult", "Elderly"], "gender": ["Female", "Male"],
                "expression": ["Angry", "Disgust", "Fear", "Happy", "Sad", "Surprise", "Neutral"]}
    for task, labels in expected.items():
        path = folder / task / f"{task}_android.tflite"
        settings = json.loads((folder / task / "model_settings.json").read_text(encoding="utf-8"))
        actual_labels = (folder / task / "labels.txt").read_text(encoding="utf-8").splitlines()
        with path.open("rb") as stream:
            header = stream.read(8)
        if header[4:8] != b"TFL3":
            raise RuntimeError("This is not a TensorFlow Lite model: " + task)
        if path.stat().st_size != manifest[task]["bytes"] or file_hash(path) != manifest[task]["sha256"]:
            raise RuntimeError("The bundled model fingerprint is wrong: " + task)
        if (actual_labels != labels or settings["class_names"] != labels
                or settings["model_task"] != task or manifest[task]["class_names"] != labels):
            raise RuntimeError("Model labels do not match: " + task)
        if (settings["input"]["shape"] != [1, 224, 224, 3]
                or settings["output"]["shape"] != [1, len(labels)]
                or settings["chosen_format"] != "FP32"):
            raise RuntimeError("Unexpected model input/output rules: " + task)
        say("Model verified: " + task)
    return manifest


def read_model_bundle(source: Path, task: str, expected: dict[str, Any],
                      expected_settings: dict[str, Any]) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Read one local model and its matching labels and settings."""
    source = Path(source).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(
            f"{task.title()} model path was not found: {source}\n"
            "Change its path at the start of this script. Do not retrain the model.")
    model_name = expected["file"]
    limits = {model_name: expected["bytes"], "labels.txt": 4096,
              "model_settings.json": 1024 * 1024, "format_validation.csv": 1024 * 1024}
    payload: dict[str, bytes] = {}
    origin: dict[str, Any] = {"path": str(source), "task": task}
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 50000 or sum(p.file_size for p in entries) > 2 * 1024 ** 3:
                raise ValueError("The model ZIP is larger than expected: " + str(source))
            members = {}
            seen = set()
            for entry in entries:
                # We read selected files only. Nothing from this ZIP is executed.
                safe_zip_path(source.parent / ".edgeface_zip_check", entry.filename)
                name = PurePosixPath(entry.filename.replace("\\", "/")).as_posix()
                if name.casefold() in seen:
                    raise ValueError("Repeated model ZIP path: " + name)
                seen.add(name.casefold())
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1:
                    raise ValueError("Linked, special or encrypted ZIP entries are not allowed.")
                if not entry.is_dir():
                    members[name] = entry
            matches = [name for name in members if PurePosixPath(name).name == model_name]
            if len(matches) != 1:
                raise ValueError(
                    f"Expected one {model_name} in {source.name}; found {len(matches)}. "
                    "Use the final Android bundle, not a mixed backup ZIP.")
            prefix = PurePosixPath(matches[0]).parent
            for name, maximum in limits.items():
                member_name = (prefix / name).as_posix()
                entry = members.get(member_name)
                if entry is None:
                    raise FileNotFoundError(f"Missing {name} beside {model_name} in {source.name}.")
                if not 0 < entry.file_size <= maximum:
                    raise ValueError("Unexpected file size: " + member_name)
                with archive.open(entry) as stream:
                    data = stream.read(maximum + 1)
                if len(data) != entry.file_size or len(data) > maximum:
                    raise ValueError("Incomplete or oversized model ZIP entry: " + member_name)
                payload[name] = data
            origin["model_member"] = matches[0]
    else:
        if source.is_dir():
            matches = [p for p in source.rglob(model_name) if p.is_file() and not p.is_symlink()]
            if len(matches) != 1:
                raise ValueError(f"Expected one {model_name} under {source}; found {len(matches)}.")
            model_file = matches[0]
        elif source.is_file() and source.suffix.lower() == ".tflite":
            model_file = source
        else:
            raise ValueError("Use a result ZIP, an extracted model folder or a .tflite file.")
        for name, maximum in limits.items():
            path = model_file if name == model_name else model_file.parent / name
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError("Missing or linked model file: " + str(path))
            if not 0 < path.stat().st_size <= maximum:
                raise ValueError("Unexpected model file size: " + str(path))
            with path.open("rb") as stream:
                payload[name] = stream.read(maximum + 1)
            if len(payload[name]) > maximum:
                raise ValueError("Model file became larger while reading: " + str(path))
        origin["model_file"] = str(model_file)

    model = payload[model_name]
    digest = hashlib.sha256(model).hexdigest()
    if len(model) != expected["bytes"] or model[4:8] != b"TFL3" or digest != expected["sha256"]:
        raise ValueError(f"{task.title()} is not the approved model. Its fingerprint does not match.")
    settings = json.loads(payload["model_settings.json"].decode("utf-8-sig"))
    labels = payload["labels.txt"].decode("utf-8-sig").splitlines()
    if not isinstance(settings, dict) or labels != expected["class_names"]:
        raise ValueError("The local model labels are wrong: " + task)
    # Keep the model's input rules paired with the model that was tested.
    for key in ("model_task", "architecture", "class_names", "input", "output",
                "chosen_format", "pixel_rule", "image_rule", "includes_face_detector"):
        if key not in settings or settings[key] != expected_settings[key]:
            raise ValueError(f"The {task} model settings do not match the approved {key}.")
    if settings.get("model_sha256", digest) != digest:
        raise ValueError("The model fingerprint inside its settings is wrong: " + task)
    report = list(csv.DictReader(io.StringIO(payload["format_validation.csv"].decode("utf-8-sig"))))
    rows = [row for row in report if row.get("format", "").strip() == "FP32"]
    if len(rows) != 1 or rows[0].get("passed", "").strip().lower() != "true":
        raise ValueError("No passing FP32 conversion check was found for " + task)
    origin.update({"model_sha256": digest, "model_bytes": len(model), "format": "FP32",
                   "companion_sha256": {name: hashlib.sha256(data).hexdigest()
                                        for name, data in payload.items() if name != model_name}})
    return payload, origin


def archive_retired_sources(project: Path) -> None:
    """Keep old generated files out of the next app build without losing their source."""
    retired = (
        "app/src/main/java/com/wpretorius/edgeface/core/Study.kt",
        "app/src/main/java/com/wpretorius/edgeface/data/StudyStore.kt",
    )
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for name in retired:
        original = project / name
        if original.is_symlink():
            raise ValueError("An old generated source is a link: " + name)
        original = safe_zip_path(project, name)
        if original.exists():
            if not original.is_file():
                raise ValueError("An old generated source is not a file: " + name)
            backup = safe_zip_path(project, ".edgeface-retired/" + stamp + "/" + name)
            backup.parent.mkdir(parents=True, exist_ok=True)
            original.replace(backup)
            say("Archived old generated source: " + name)


def write_project(project: Path, version_code: int, model_paths: dict[str, Path]) -> dict[str, str]:
    """Write the app and copy verified models from the local paths."""
    sources = app_source_files()
    manifest = json.loads(sources["app/src/main/assets/models/manifest.json"])
    if set(model_paths) != set(manifest):
        raise ValueError("Provide the age, gender and expression model paths.")
    bundles = {}
    records = []
    # Check every input before changing the build workspace.
    for task in ("age", "gender", "expression"):
        settings = json.loads(sources[f"app/src/main/assets/models/{task}/model_settings.json"])
        bundles[task], record = read_model_bundle(model_paths[task], task, manifest[task], settings)
        records.append(record)
        say("Local model verified: " + task)
    project.mkdir(parents=True, exist_ok=True)
    archive_retired_sources(project)
    names = set()
    for name, source in sources.items():
        if name == "app/build.gradle.kts":
            source = source.replace("versionCode = 1", f"versionCode = {version_code}")
        atomic_write(safe_zip_path(project, name), source.encode("utf-8"))
        names.add(name)
    for task, bundle in bundles.items():
        for name, content in bundle.items():
            if name == "format_validation.csv":
                target = f"docs/model_evidence/{task}/{name}"
            else:
                target = f"app/src/main/assets/models/{task}/{name}"
            atomic_write(safe_zip_path(project, target), content)
            names.add(target)
    # Only old generated test pictures are removed; original model inputs stay unchanged.
    for index in range(12):
        old_fixture = project / f"app/src/test/resources/resize_fixtures/case_{index:02d}.bin"
        if old_fixture.is_file() and not old_fixture.is_symlink():
            old_fixture.unlink()
    record_path = "docs/local_model_inputs.json"
    save_json(project / record_path, {"builder_version": BUILDER_VERSION, "models": records})
    names.add(record_path)
    verify_models(project)
    # Do not collect unrelated old files from the build cache.
    return {name: file_hash(safe_zip_path(project, name)) for name in sorted(names)}


def save_source_copy(project: Path, destination: Path, names: list[str]) -> None:
    """Keep readable source for the professor. Do not copy private keys or caches."""
    destination.mkdir(parents=True, exist_ok=False)
    for name in names:
        source = safe_zip_path(project, name)
        if source.is_file():
            target = safe_zip_path(destination, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def protect_key_folder(folder: Path) -> None:
    """Make the new signing folder private to this Windows account."""
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        info = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, errors="replace", check=True)
        match = re.search(r"S-1-[\d-]+", info.stdout)
        if not match:
            raise RuntimeError("Could not find this Windows account's security ID.")
        sid = match.group(0)
        # Only this dedicated app-signing folder is changed.
        run_command(["icacls", str(folder), "/inheritance:r", "/grant:r",
                     f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"])
    else:
        folder.chmod(0o700)


def prepare_signing_key(java: Path, folder: Path, env: dict[str, str]) -> tuple[Path, dict[str, str]]:
    """Create a local release key once, then reuse it for app updates."""
    protect_key_folder(folder)
    credentials_path = folder / "private_signing.json"
    key = folder / "edgeface-release.p12"
    pending = folder / "edgeface-pending.p12"
    if credentials_path.exists():
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
        if not credentials.get("password") or credentials.get("alias") != "edgeface":
            raise RuntimeError("The private signing settings are incomplete. Restore their backup.")
        if not key.is_file() and credentials.get("state") != "creating":
            raise RuntimeError("The saved release key is missing. Restore your private key backup; do not create a new key.")
    else:
        if key.exists() or pending.exists():
            raise RuntimeError("An existing release key has no matching settings. It will not be overwritten.")
        credentials = {"alias": "edgeface", "password": secrets.token_urlsafe(36), "state": "creating",
                       "created": datetime.now(timezone.utc).isoformat()}
        # Save the password before generating the key, so an interrupted build can recover it.
        save_json(credentials_path, credentials)
        if os.name != "nt":
            credentials_path.chmod(0o600)
    signed_env = env.copy()
    signed_env["EDGEFACE_SIGN_PASS"] = credentials["password"]
    suffix = ".exe" if os.name == "nt" else ""
    keytool = java / "bin" / ("keytool" + suffix)
    if not key.is_file():
        if not pending.exists():
            run_command([keytool, "-genkeypair", "-noprompt",
                         "-alias", "edgeface", "-keyalg", "RSA", "-keysize", "3072",
                         "-validity", "10000", "-storetype", "PKCS12", "-keystore", pending,
                         "-storepass:env", "EDGEFACE_SIGN_PASS", "-keypass:env", "EDGEFACE_SIGN_PASS",
                         "-dname", "CN=W. Pretorius, OU=EdgeFace, O=Academic Project"], env=signed_env)
        if not pending.is_file():
            raise RuntimeError("Java did not create the signing key.")
        # A partial key must pass this check before it becomes the release key.
        run_command([keytool, "-list", "-keystore", pending, "-alias", "edgeface",
                     "-storepass:env", "EDGEFACE_SIGN_PASS"], env=signed_env)
        pending.replace(key)
    run_command([keytool, "-list", "-keystore", key,
                 "-alias", "edgeface", "-storepass:env", "EDGEFACE_SIGN_PASS"], env=signed_env)
    credentials["state"] = "active"
    save_json(credentials_path, credentials)
    if os.name != "nt":
        key.chmod(0o600)
        credentials_path.chmod(0o600)
    say("Private signing key: " + str(folder))
    say("Keep and back up that PRIVATE folder. Never send it with the APK.")
    return key, signed_env


def build_unsigned_apk(java: Path, sdk: Path, project: Path, env: dict[str, str]) -> Path:
    """Run the app tests, then build a release APK."""
    sdk_text = str(sdk.resolve()).replace("\\", "/").replace(":", "\\:")
    atomic_write(project / "local.properties", ("sdk.dir=" + sdk_text + "\n").encode("utf-8"))
    previous = project / "app/build/outputs/apk/release/app-release-unsigned.apk"
    if previous.exists():
        previous.unlink()  # A previous APK must not be reported as this build's output.
    run_command([java / "bin/java.exe", "-Dfile.encoding=UTF-8", "-cp",
                 project / "gradle/wrapper/gradle-wrapper.jar", "org.gradle.wrapper.GradleWrapperMain",
                 "--no-daemon", "--console=plain", "--max-workers=2", "--stacktrace",
                 "-Dorg.gradle.java.home=" + str(java), ":app:testReleaseUnitTest", ":app:assembleRelease"],
                cwd=project, env=env)
    if not previous.is_file() or not zipfile.is_zipfile(previous):
        raise RuntimeError("The build did not produce the expected unsigned APK.")
    return previous


def sign_apk(java: Path, sdk: Path, unsigned: Path, key: Path,
             env: dict[str, str]) -> Path:
    """Align the APK first, then sign it with the local release key."""
    tools = sdk / "build-tools" / SDK_BUILD_TOOLS
    aligned = unsigned.with_name("edgeface-aligned.apk")
    signed = unsigned.with_name("edgeface-signed.apk")
    run_command([tools / "zipalign.exe", "-P", "16", "-f", "4", unsigned, aligned], env=env)
    run_command([java / "bin/java.exe", "-jar", tools / "lib/apksigner.jar", "sign",
                 "--ks", key, "--ks-key-alias", "edgeface", "--ks-pass", "env:EDGEFACE_SIGN_PASS",
                 "--key-pass", "env:EDGEFACE_SIGN_PASS", "--v2-signing-enabled", "true",
                 "--v3-signing-enabled", "true", "--v4-signing-enabled", "false",
                 "--out", signed, aligned], env=env)
    if not signed.is_file():
        raise RuntimeError("The signing tool did not produce an APK.")
    return signed


def verify_apk_assets(apk: Path, manifest: dict[str, Any]) -> list[str]:
    """Make sure the signed app still contains all three exact model files."""
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        if "AndroidManifest.xml" not in names or "classes.dex" not in names:
            raise RuntimeError("The file is not a complete Android APK.")
        for task, expected in manifest.items():
            name = f"assets/models/{task}/{task}_android.tflite"
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != expected["sha256"] or len(data) != expected["bytes"]:
                raise RuntimeError("The APK contains the wrong model: " + task)
            if archive.getinfo(name).compress_type != zipfile.ZIP_STORED:
                raise RuntimeError("A model was compressed; Android memory mapping would fail.")
        abis = sorted({name.split("/")[1] for name in names if name.startswith("lib/") and name.endswith(".so")})
        if "arm64-v8a" not in abis:
            raise RuntimeError("The APK is missing its ARM64 native libraries.")
        return abis


def read_apk_identity(badging: str) -> dict[str, Any]:
    """Check the app name and Android version without depending on one spelling."""
    package_lines = re.findall(r"(?m)^[ \t]*package:[ \t]+([^\r\n]+)", badging)
    minimums = re.findall(
        r"(?m)^[ \t]*(?:minSdkVersion|sdkVersion):[ \t]*'(\d+)'[ \t]*\r?$",
        badging,
    )
    if len(package_lines) != 1:
        raise RuntimeError("Could not read exactly one APK package entry.")
    names = re.findall(r"(?:^|[ \t])name='([^']+)'", package_lines[0])
    versions = re.findall(r"(?:^|[ \t])versionCode='(\d+)'", package_lines[0])
    if names != [APP_ID]:
        raise RuntimeError(f"Wrong app ID. Expected {APP_ID}; found {names}.")
    if not minimums or any(int(value) != 26 for value in minimums):
        raise RuntimeError(f"Wrong minimum Android API. Expected 26; found {minimums}.")
    if len(versions) != 1 or not 1 <= int(versions[0]) <= 2100000000:
        raise RuntimeError("The APK has no valid Android version code.")
    return {"app_id": names[0], "minimum_android_api": 26,
            "version_code": int(versions[0])}


def verify_signed_apk(java: Path, sdk: Path, apk: Path, project: Path,
                      env: dict[str, str]) -> dict[str, Any]:
    """Check the signature, alignment, permissions and model fingerprints."""
    tools = sdk / "build-tools" / SDK_BUILD_TOOLS
    certificate = run_command([java / "bin/java.exe", "-jar", tools / "lib/apksigner.jar",
                              "verify", "--verbose", "--print-certs", "--min-sdk-version", "26", apk], env=env)
    run_command([tools / "zipalign.exe", "-c", "-P", "16", "4", apk], env=env)
    permissions = run_command([tools / "aapt2.exe", "dump", "permissions", apk], env=env)
    badging = run_command([tools / "aapt2.exe", "dump", "badging", apk], env=env)
    if "android.permission.INTERNET" in permissions:
        raise RuntimeError("The built APK unexpectedly requests Internet access.")
    identity = read_apk_identity(badging)
    if "application-debuggable" in badging:
        raise RuntimeError("A debug APK was produced instead of the requested release.")
    manifest = json.loads((project / "app/src/main/assets/models/manifest.json").read_text(encoding="utf-8"))
    abis = verify_apk_assets(apk, manifest)
    with zipfile.ZipFile(apk) as archive:
        for task in manifest:
            for name in ("labels.txt", "model_settings.json"):
                original = project / "app/src/main/assets/models" / task / name
                if archive.read(f"assets/models/{task}/{name}") != original.read_bytes():
                    raise RuntimeError("The APK contains changed model settings: " + task)
    return {"signature_verified": True, "alignment_verified": True, "internet_permission": False,
            "model_fingerprints_verified": True, "native_abis": abis,
            **identity, "apk_sha256": file_hash(apk),
            "certificate_output": certificate, "phone_camera_tested": False,
            "phone_inference_tested": False, "apk_bytes": apk.stat().st_size}


def acquire_build_lock(cache: Path):
    """Do not let two copies overwrite the same build workspace."""
    lock = (cache / "build.lock").open("a+b")
    lock.seek(0)
    lock.write(b"0")
    lock.flush()
    lock.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise RuntimeError("Another EdgeFace build is already running. Let it finish first.")
    return lock


def show_output_folder(folder: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(folder))
    except OSError:
        pass


def parse_arguments() -> argparse.Namespace:
    """Use the paths at the top unless a command-line option replaces one."""
    parser = argparse.ArgumentParser(description="Build EdgeFace using the local model paths.")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Optional folder for the APK, build logs and readable source.")
    parser.add_argument("--age-model", type=Path, default=AGE_MODEL_PATH)
    parser.add_argument("--gender-model", type=Path, default=GENDER_MODEL_PATH)
    parser.add_argument("--expression-model", type=Path, default=EXPRESSION_MODEL_PATH)
    parser.add_argument("--cache-dir", type=Path, default=BUILD_CACHE_DIR)
    parser.add_argument("--java-home", type=Path, default=JAVA_HOME_PATH)
    parser.add_argument("--sdk-dir", type=Path, default=ANDROID_SDK_PATH)
    parser.add_argument("--extract-only", action="store_true",
                        help="Check models and write source only. Do not download tools or build an APK.")
    return parser.parse_args()


def app_source_files() -> dict[str, str]:
    """Return the readable Android source files."""
    return {
        '.gitignore': r'''.gradle/
.idea/
local.properties
**/build/
*.jks
*.keystore
keystore.properties
# Do not publish weights or private participant data before their release terms are checked.
app/src/main/assets/models/**/*.tflite
app/src/main/assets/models/**/model_settings.json
app/src/main/assets/models/**/labels.txt
*.csv
*.apk
*.download
*.zip
study_images/

*.p12
private_signing.json

.edgeface-retired/

.edgeface-retired/
''',
        'REQUIREMENTS_AND_TESTS.md': r'''# Task 2 - current implementation and checks

## Application scope

The app has Analyze, Models and Help screens. Camera, gallery and file import use
the same three approved models. The benchmark remains on Models, with separate CSV
and JSON export. There is no built-in 20-image collection form or study database.
Photos are not automatically uploaded. The release manifest must have no Internet permission.

## Report evidence kept outside the app

The task still calls for 20 images captured with the device, with 10 Adult and 10
Elderly images and an equal Female/Male and Happy/Sad split within each age group.
Use screenshots to support a separate results table. Before predictions, record the
reference labels and anonymous image IDs outside the app. Record mistakes and detection
failures too. Do not substitute a model score or benchmark time for classification accuracy.

Adult 20-59 and Elderly 60+ are project definitions. Predictions of dataset gender labels
are not gender-identity claims. Expression labels are not a person's internal feelings.
Faces in screenshots remain identifiable. Get permission and keep them private.

The report must still describe AI-tool use, the real development process and limitations,
as requested in the supplied course task. This report information need not appear as
an assistance credit on the app screen. Dataset/library credits remain unchanged.

## Benchmark method

A fixed grey 224 x 224 RGB input, two CPU threads, five warm-ups and 30 measured calls
per model. Median/p95 cover Interpreter invocation only, not loading, detection, resizing
or camera time. Model fingerprints, device, app version, method and recorded UTC time
are included in the exports. A legacy benchmark without a time stays marked unknown.
Original Keras holdout metrics are separately labelled, not presented as phone accuracy.

## Checks before distribution

1. Run release unit tests and build/sign/verify the APK with the builder.
2. Install the exact release on a phone. Test startup in airplane mode.
3. Test front/back camera, permission denial, camera cancellation and system-camera fallback.
4. Test gallery and Files, cancellation, revoked permissions, JPEG/PNG and corrupt/huge images.
5. Test rotation and process recreation while the external picker or camera is open.
6. Check single/no/multiple/tiny faces and readable errors without a crash.
7. Capture screenshots and check that the photo, predictions, timing and version are readable.
8. Run a phone benchmark. Export both CSV and JSON using the buttons on Models.
9. Reopen the app and check saved benchmark results. Check their hashes after app updates.
10. Test legacy benchmark loading and malformed benchmark files; do not invent lost results.
11. Confirm no participant collection UI remains and new analysis images are not kept as a history.
12. Run connectedDebugAndroidTest on a device/emulator. Device tests are not claimed by core tests.

The new screen flow, actual photo input and Android exports still require phone tests.
No real device measurements or evaluation observations are generated during script preparation.
''',
        'SOURCES.md': r'''# Sources and attribution

## Supplied project sources

- IU International University. *DLBAIPEAI – Project Edge AI: Task – Project report*, Task 2, pp. 3–4. Requirement interpretation is based on the user's supplied document, not a replacement public specification.
- IU International University. *Guidelines for the Creation of a Project Report*, pp. 1–4. Practical product, documented process, evaluation, reflection and formal report requirements.
- W. Pretorius. `age_results_20260915_044643.zip`, `gender_results_20260915_030921.zip`, `expression_android_bundle_20260915_142717_617660.zip`. Exact model files, metadata, labels and evaluation CSVs used in this app.
- W. Pretorius. `03_Expression_Model_Colab.pdf`. The recovery/validation section selects FP32 and rejects INT8. The Android bundle is the authoritative deployable expression artifact; the recovery archive is not substituted for it.

## Technical sources checked during implementation

- Android Developers. CameraX release notes and camera-library guidance. https://developer.android.com/jetpack/androidx/releases/camera and https://developer.android.com/media/camera/choose-camera-library
- Google. ML Kit face detection for Android. Bundled `com.google.mlkit:face-detection:16.1.7`. https://developers.google.com/ml-kit/vision/face-detection/android
- Google. LiteRT for Android. Standalone Interpreter artifact `com.google.ai.edge.litert:litert:1.4.2`. https://developers.google.com/edge/litert/android
- Android Developers. Photo picker. https://developer.android.com/training/data-storage/shared/photo-picker
- Android Developers. Configure build variants and signing. https://developer.android.com/build/build-variants
- Android Developers. Build from the command line. https://developer.android.com/build/building-cmdline
- Android Developers. Android Gradle Plugin 8.13 release notes. https://developer.android.com/build/releases/agp-8-13-0-release-notes
- Gradle. Wrapper and checksum reference. https://docs.gradle.org/current/userguide/gradle_wrapper.html and https://gradle.org/release-checksums/
- Pillow contributors. RGB bilinear coefficient/rounding behavior, `Resample.c` in release 11.1.0. https://github.com/python-pillow/Pillow/blob/11.1.0/src/libImaging/Resample.c

The Kotlin RGB resizer is a small implementation of the separable bilinear rule, with widened support for downscaling and 22-bit rounded coefficients. It was tested against the installed Pillow 12.3.0 using 12 deterministic synthetic cases. No Pillow binary or font file is included in the APK.

## Model/data background

- Kärkkäinen, K., & Joo, J. (2021). FairFace: Face attribute dataset for balanced race, gender, and age for bias measurement and mitigation. *WACV*. https://github.com/joojs/fairface
- Zhang, Z., Luo, P., Loy, C. C., & Tang, X. (2018). From facial expression recognition to interpersonal relation prediction. *International Journal of Computer Vision, 126*, 550–569. https://doi.org/10.1007/s11263-017-1055-1
- Howard et al. (2019). Searching for MobileNetV3. https://arxiv.org/abs/1905.02244
- Tan, M., & Le, Q. V. (2021). EfficientNetV2: Smaller models and faster training. https://proceedings.mlr.press/v139/tan21a.html

## Distribution caution

Original data/model/library terms remain applicable. A Kaggle mirror's license label is not proof of unrestricted rights in underlying images or a public commercial model release. This package is prepared for private academic testing and examiner distribution. Verify applicable terms before public distribution. Do not upload the IU task sheets, sample training photos, participant faces or signing keys with the project.

AndroidX/CameraX, Kotlin/coroutines, LiteRT and Gradle retain their respective notices and licenses; ML Kit has Google's SDK terms. Follow those terms for a distributed APK. No independent license grant for the user's trained model weights is made here.

Kotlin build compatibility (consulted 15 September 2026): https://kotlinlang.org/docs/gradle-configure-project.html
Kotlin Android and matching Compose compiler plugin 2.3.10 are pinned. This version's documented Gradle/AGP ranges cover Gradle 8.13 and AGP 8.13.2. The build uses the compilerOptions JVM-target DSL instead of the deprecated kotlinOptions block.


## Readable local-model builder revision

The three models are read from the user's local result ZIPs or model folders. Their original
approved hashes, labels and tensor rules are preserved. No model is retrained or downloaded.
The original 12 binary resizing fixtures are replaced by 12 deterministic synthetic test inputs.
Their expected RGB SHA-256 values were calculated independently with Pillow 12.3.0,
using `Image.Resampling.BILINEAR`. The Android pixel preparation code is unchanged.
The recipe and reference hashes are recorded in the test resources and `CoreChecks.kt`.

## Photo import

Android Developers. Photo picker. https://developer.android.com/training/data-storage/shared/photo-picker

Android Developers. Access documents and other files from shared storage. https://developer.android.com/training/data-storage/shared/documents-files

The app uses the system image picker and an image-only OpenDocument picker. Gallery/files require no camera or whole-library storage permission. Decode/crop/resize and all three predictions stay on the phone; eligible cloud providers may need to download the selected image first.


## Independent benchmark export

Android Developers. ActivityResultContracts.CreateDocument. https://developer.android.com/reference/androidx/activity/result/contract/ActivityResultContracts.CreateDocument

Android Developers. ContentResolver.openOutputStream. https://developer.android.com/reference/android/content/ContentResolver

CSV and JSON benchmark exports use the user-chosen destination. They contain measurements,
model fingerprints and device details, not photographs. The benchmark method and model files
are unchanged. Collection and report preparation are now external to the app.
''',
        'app/build.gradle.kts': r'''import java.security.MessageDigest
import groovy.json.JsonSlurper
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}
android {
    namespace = "com.wpretorius.edgeface"
    compileSdk = 36
    buildToolsVersion = "35.0.0"
    defaultConfig {
        applicationId = "com.wpretorius.edgeface"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.2.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    buildTypes {
        debug { applicationIdSuffix = ".debug"; versionNameSuffix = "-preview" }
        release {
            isMinifyEnabled = false
            // The Python builder signs this release with a key kept on this computer.
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures { compose = true; buildConfig = true }
    androidResources { noCompress += "tflite" }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}

kotlin { compilerOptions { jvmTarget.set(JvmTarget.JVM_17) } }

// Both debug and release must contain the exact approved models.
val verifyModels by tasks.registering {
    val assets = file("src/main/assets/models")
    inputs.dir(assets)
    doLast {
        @Suppress("UNCHECKED_CAST")
        val manifest = JsonSlurper().parse(file("$assets/manifest.json")) as Map<String, Map<String, Any>>
        listOf("age", "gender", "expression").forEach { task ->
            listOf("${task}_android.tflite", "model_settings.json", "labels.txt").forEach { name ->
                check(file("$assets/$task/$name").isFile) { "Missing model asset: $task/$name" }
            }
            val model = file("$assets/$task/${task}_android.tflite")
            val hash = MessageDigest.getInstance("SHA-256").digest(model.readBytes())
                .joinToString("") { "%02x".format(it.toInt() and 255) }
            check(hash == manifest.getValue(task)["sha256"]) { "Model fingerprint mismatch: $task" }
        }
    }
}
tasks.matching { it.name == "preBuild" }.configureEach { dependsOn(verifyModels) }

dependencies {
    implementation(platform("androidx.compose:compose-bom:2025.05.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.core:core-ktx:1.16.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.9.1")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.1")
    implementation("androidx.exifinterface:exifinterface:1.4.2")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("androidx.camera:camera-camera2:1.6.1")
    implementation("androidx.camera:camera-lifecycle:1.6.1")
    implementation("androidx.camera:camera-view:1.6.1")
    // Bundled detector: no first-use model download.
    implementation("com.google.mlkit:face-detection:16.1.7")
    // Standalone Interpreter API. Inference uses the phone CPU, not a cloud server.
    implementation("com.google.ai.edge.litert:litert:1.4.2")
    debugImplementation("androidx.compose.ui:ui-tooling")
    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
}
''',
        'app/src/androidTest/java/com/wpretorius/edgeface/DeviceChecks.kt': r'''package com.wpretorius.edgeface

import android.content.ContextWrapper
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Color
import android.net.Uri
import androidx.exifinterface.media.ExifInterface
import androidx.core.content.FileProvider
import com.wpretorius.edgeface.ml.CaptureProblem
import java.security.MessageDigest
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.wpretorius.edgeface.core.*
import com.wpretorius.edgeface.data.BenchmarkStore
import com.wpretorius.edgeface.ml.ModelRunner
import com.wpretorius.edgeface.ml.PhotoInput
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

// Run these on a phone or emulator. They are not replaced by the desktop logic tests.
@RunWith(AndroidJUnit4::class)
class DeviceChecks {
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test fun bundledModelsReallyRun() {
        for (a in Attribute.values()) ModelRunner(context, a).use { runner ->
            val p = runner.predict(FloatArray(224 * 224 * 3) { i -> (i % 256).toFloat() })
            assertEquals(a.expectedLabels, p.scores.map { it.first }.toSet())
            assertTrue(p.modelMs >= 0 && p.modelHash.length == 64)
            assertEquals("FP32", p.format)
        }
    }
    @Test fun appHasNoNetworkPermission() {
        @Suppress("DEPRECATION")
        val permissions = context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS).requestedPermissions.orEmpty()
        assertFalse(permissions.contains("android.permission.INTERNET"))
        assertFalse(permissions.contains("android.permission.ACCESS_NETWORK_STATE"))
    }
    @Test fun exifRotatedPhotographLoadsUpright() {
        val file = File(context.cacheDir, "exif_test_${UUID.randomUUID()}.jpg")
        val image = Bitmap.createBitmap(640, 480, Bitmap.Config.ARGB_8888).apply { eraseColor(Color.RED) }
        try {
            file.outputStream().use { assertTrue(image.compress(Bitmap.CompressFormat.JPEG, 100, it)) }
            ExifInterface(file).apply { setAttribute(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_ROTATE_90.toString()); saveAttributes() }
            val decoded = PhotoInput.decode(context, Uri.fromFile(file))
            assertEquals(480, decoded.width); assertEquals(640, decoded.height)
            assertEquals(Bitmap.Config.ARGB_8888, decoded.config)
            decoded.recycle()
        } finally { image.recycle(); file.delete() }
    }
    @Test fun libraryPngReadsFromContentUriAndDoesNotChangeOriginal() {
        val folder = File(context.cacheDir, "captures").apply { mkdirs() }
        val file = File(folder, "gallery_${UUID.randomUUID()}.png")
        val image = Bitmap.createBitmap(320, 240, Bitmap.Config.ARGB_8888).apply { eraseColor(Color.BLUE) }
        try {
            file.outputStream().use { assertTrue(image.compress(Bitmap.CompressFormat.PNG, 100, it)) }
            val before = MessageDigest.getInstance("SHA-256").digest(file.readBytes())
            val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", file)
            val loaded = PhotoInput.decode(context, uri)
            try {
                assertEquals(320, loaded.width)
                assertEquals(240, loaded.height)
                assertEquals(Color.BLUE, loaded.getPixel(100, 100))
                assertArrayEquals(before, MessageDigest.getInstance("SHA-256").digest(file.readBytes()))
            } finally { loaded.recycle() }
        } finally { image.recycle(); file.delete() }
    }
    @Test fun rotatedLibraryJpegReadsFromContentUri() {
        val folder = File(context.cacheDir, "captures").apply { mkdirs() }
        val file = File(folder, "gallery_rotation_${UUID.randomUUID()}.jpg")
        val image = Bitmap.createBitmap(640, 480, Bitmap.Config.ARGB_8888).apply { eraseColor(Color.GREEN) }
        try {
            file.outputStream().use { assertTrue(image.compress(Bitmap.CompressFormat.JPEG, 100, it)) }
            ExifInterface(file).apply {
                setAttribute(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_ROTATE_90.toString())
                saveAttributes()
            }
            val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", file)
            val loaded = PhotoInput.decode(context, uri)
            try { assertEquals(480, loaded.width); assertEquals(640, loaded.height) }
            finally { loaded.recycle() }
        } finally { image.recycle(); file.delete() }
    }
    @Test fun invalidLibraryFileGivesAReadableError() {
        val folder = File(context.cacheDir, "captures").apply { mkdirs() }
        val file = File(folder, "not_an_image_${UUID.randomUUID()}.jpg")
        try {
            file.writeText("This is not a photograph.")
            val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", file)
            try {
                PhotoInput.decode(context, uri)
                fail("A broken image must not be accepted.")
            } catch (error: CaptureProblem) {
                assertTrue(error.code in setOf("IMAGE_READ", "IMAGE_FORMAT"))
                assertTrue(error.message.orEmpty().contains("JPEG"))
            }
        } finally { file.delete() }
    }
    @Test fun libraryDoesNotNeedWholeStoragePermission() {
        @Suppress("DEPRECATION")
        val permissions = context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS).requestedPermissions.orEmpty()
        for (name in listOf("READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "READ_MEDIA_IMAGES", "MANAGE_EXTERNAL_STORAGE")) {
            assertFalse(permissions.contains("android.permission.$name"))
        }
    }
    @Test fun benchmarkRoundTripAndExport() {
        val testDirectory = File(context.cacheDir, "benchmark_test_${UUID.randomUUID()}").apply { mkdirs() }
        val isolated = object : ContextWrapper(context) { override fun getFilesDir(): File = testDirectory }
        try {
            val store = BenchmarkStore(isolated)
            assertNull(store.read())
            val rows = Attribute.values().map { BenchmarkResult(it, 30, 2.5, 4.0, 2, "a".repeat(64)) }
            val report = BenchmarkReport("Test phone", "1.2.0", "2026-09-16T00:00:00Z", rows)
            store.save(report)
            assertEquals(report, store.read())
            assertEquals(3, org.json.JSONObject(store.asJson(report)).getJSONArray("results").length())
            assertTrue(BenchmarkCsv.export(report).contains("median_ms"))
            assertEquals(setOf("benchmark.json"), testDirectory.listFiles().orEmpty().map { it.name }.toSet())
        } finally { testDirectory.deleteRecursively() }
    }
    @Test fun oldBenchmarkLoadsWithoutInventingATime() {
        val testDirectory = File(context.cacheDir, "benchmark_old_${UUID.randomUUID()}").apply { mkdirs() }
        val isolated = object : ContextWrapper(context) { override fun getFilesDir(): File = testDirectory }
        try {
            val store = BenchmarkStore(isolated)
            val rows = Attribute.values().map { BenchmarkResult(it, 30, 2.5, 4.0, 2, "a".repeat(64)) }
            val report = BenchmarkReport("Test phone", "1.1.0", null, rows)
            val old = org.json.JSONObject(store.asJson(report)).apply { remove("measured_at_utc") }
            File(testDirectory, "benchmark.json").writeText(old.toString())
            assertEquals(report, store.read())
        } finally { testDirectory.deleteRecursively() }
    }
}
''',
        'app/src/main/AndroidManifest.xml': r'''<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    xmlns:tools="http://schemas.android.com/tools">
    <!-- Models are packaged in the APK. Remove networking permissions contributed by libraries. -->
    <uses-permission android:name="android.permission.INTERNET" tools:node="remove" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" tools:node="remove" />
    <uses-permission android:name="android.permission.CAMERA" />
    <uses-feature android:name="android.hardware.camera" android:required="false" />
    <uses-feature android:name="android.hardware.camera.autofocus" android:required="false" />
    <uses-feature android:name="android.hardware.camera.any" android:required="false" />
    <queries>
        <intent><action android:name="android.media.action.IMAGE_CAPTURE" /></intent>
    </queries>
    <application android:allowBackup="false" android:fullBackupContent="false"
        android:dataExtractionRules="@xml/data_extraction_rules"
        android:label="EdgeFace" android:icon="@drawable/ic_app"
        android:supportsRtl="true" android:theme="@style/Theme.EdgeFace"
        android:usesCleartextTraffic="false">
        <activity android:name=".MainActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
        <provider android:name="androidx.core.content.FileProvider"
            android:authorities="${applicationId}.files"
            android:exported="false" android:grantUriPermissions="true">
            <meta-data android:name="android.support.FILE_PROVIDER_PATHS" android:resource="@xml/file_paths" />
        </provider>
    </application>
</manifest>
''',
        'app/src/main/assets/evaluation/training_results.json': r'''{
  "age": {
    "holdout": {
      "accuracy": 0.9118685331710286,
      "balanced_accuracy": 0.8663997101062084,
      "macro_f1": 0.7244616781558075
    },
    "scope": "Original saved Keras holdout results. Not phone accuracy.",
    "bundle": "age_results_20260915_044643.zip",
    "selected_format": "FP32",
    "conversion_validation": [
      {
        "format": "FP32",
        "size_MB": "3.735872",
        "max_abs_difference": "1.4841556549072266e-05",
        "mean_abs_difference": "1.0414356665933155e-06",
        "p99_abs_difference": "8.19221168057993e-06",
        "prediction_agreement": "1.0",
        "accuracy": "0.88671875",
        "recall_adult": "0.8984375",
        "recall_elderly": "0.875",
        "balanced_accuracy": "0.88671875",
        "balanced_accuracy_drop": "0.0",
        "largest_class_recall_drop": "0.0",
        "passed": "True",
        "reason": "All checks passed"
      }
    ],
    "age_definition": {
      "Adult": "20-59",
      "Elderly": "60+"
    }
  },
  "gender": {
    "holdout": {
      "accuracy": 0.8948378254910918,
      "balanced_accuracy": 0.8953790378510882,
      "macro_f1": 0.8946533578809162
    },
    "scope": "Original saved Keras holdout results. Not phone accuracy.",
    "bundle": "gender_results_20260915_030921.zip",
    "selected_format": "FP32",
    "conversion_validation": [
      {
        "format": "FP32",
        "size_MB": "3.731884",
        "max_abs_difference": "1.7344951629638672e-05",
        "mean_abs_difference": "1.2075004178768722e-06",
        "p99_abs_difference": "9.697381756268442e-06",
        "prediction_agreement": "1.0",
        "accuracy": "0.890625",
        "recall_female": "0.92578125",
        "recall_male": "0.85546875",
        "balanced_accuracy": "0.890625",
        "balanced_accuracy_drop": "0.0",
        "largest_class_recall_drop": "0.0",
        "passed": "True",
        "reason": "All checks passed"
      }
    ],
    "age_definition": null
  },
  "expression": {
    "holdout": {
      "accuracy": 0.6577424844015882,
      "balanced_accuracy": 0.4607444225931667,
      "macro_f1": 0.4595262816082089
    },
    "scope": "Original saved Keras holdout results. Not phone accuracy.",
    "bundle": "expression_android_bundle_20260915_142717_617660.zip",
    "selected_format": "FP32",
    "conversion_validation": [
      {
        "format": "FP32",
        "max_abs_difference": "2.4437904357910156e-06",
        "mean_abs_difference": "1.1718145742634078e-07",
        "prediction_agreement": "1.0",
        "accuracy": "0.46875",
        "balanced_accuracy": "0.46875",
        "balanced_accuracy_drop": "0.0",
        "largest_class_recall_drop": "0.0",
        "recall_angry": "0.53125",
        "recall_disgust": "0.0625",
        "recall_fear": "0.171875",
        "recall_happy": "0.734375",
        "recall_sad": "0.40625",
        "recall_surprise": "0.65625",
        "recall_neutral": "0.71875",
        "passed": "True",
        "reason": "All checks passed"
      },
      {
        "format": "INT8",
        "max_abs_difference": "0.33939051628112793",
        "mean_abs_difference": "0.027740851044654846",
        "prediction_agreement": "0.8571428571428571",
        "accuracy": "0.4419642857142857",
        "balanced_accuracy": "0.4419642857142857",
        "balanced_accuracy_drop": "0.0267857142857143",
        "largest_class_recall_drop": "0.078125",
        "recall_angry": "0.53125",
        "recall_disgust": "0.03125",
        "recall_fear": "0.09375",
        "recall_happy": "0.75",
        "recall_sad": "0.40625",
        "recall_surprise": "0.578125",
        "recall_neutral": "0.703125",
        "passed": "False",
        "reason": "max_abs_difference, largest_class_recall_drop, prediction_agreement"
      }
    ],
    "age_definition": null
  }
}''',
        'app/src/main/assets/models/age/labels.txt': r'''Adult
Elderly
''',
        'app/src/main/assets/models/age/model_settings.json': r'''{
  "course": "DLBAIPEAI",
  "task": "Task 2",
  "model_task": "age",
  "architecture": "MobileNetV3Small",
  "age_definition": {
    "Adult": "20-59",
    "Elderly": "60+"
  },
  "excluded_age_ranges": [
    "0-2",
    "3-9",
    "10-19"
  ],
  "seed": 42,
  "class_names": [
    "Adult",
    "Elderly"
  ],
  "notebook_revision": "colab-age-balanced-v1",
  "training_sampling": "exact half Adult / half Elderly; shuffled cycles; training augmentation only",
  "training_class_weights": null,
  "samples_per_epoch": 104928,
  "checkpoint_rule": "highest validation balanced accuracy; validation loss breaks stage ties",
  "decision_rule": "argmax of Adult/Elderly scores; no holdout-tuned threshold",
  "probability_note": "Balanced training changes the class prior; scores are not calibrated real-world age probabilities.",
  "reused_holdout_informed_revision": true,
  "conversion_fp32_limits": {
    "max_abs": 0.01,
    "mean_abs": 0.001,
    "min_agreement": 0.99,
    "max_balanced_accuracy_drop": 0.01,
    "max_class_recall_drop": 0.02
  },
  "conversion_int8_limits": {
    "max_abs": 0.15,
    "mean_abs": 0.02,
    "min_agreement": 0.97,
    "max_balanced_accuracy_drop": 0.02,
    "max_class_recall_drop": 0.03
  },
  "dataset_handle": "mehmoodsheikh/fairface-dataset",
  "dataset_directory": "/content/project_edge_ai/datasets/kagglehub_cache/datasets/mehmoodsheikh/fairface-dataset/versions/1",
  "input": {
    "shape": [
      1,
      224,
      224,
      3
    ],
    "dtype": "float32",
    "scale": 0.0,
    "zero_point": 0
  },
  "output": {
    "shape": [
      1,
      2
    ],
    "dtype": "float32",
    "scale": 0.0,
    "zero_point": 0
  },
  "image_rule": "Crop face, convert to RGB, resize to 224x224 using bilinear interpolation.",
  "pixel_rule": "Float pixels 0-255. Scaling is already inside the model. Do not divide by 255.",
  "integer_rule": "For integer tensors use the saved scale and zero point, with rounding and clipping.",
  "includes_face_detector": false,
  "measured_on_phone": false,
  "split_sizes": {
    "train": 55533,
    "validation": 9801,
    "holdout": 8215
  },
  "chosen_stage": "fine",
  "chosen_format": "FP32",
  "training_seconds": 4417.394436351,
  "gpu_name": "Tesla T4",
  "gpu_forward_backward_test_passed": true,
  "holdout_scores": {
    "accuracy": 0.9118685331710286,
    "balanced_accuracy": 0.8663997101062084,
    "macro_f1": 0.7244616781558075
  },
  "versions": {
    "tensorflow": "2.20.0",
    "kagglehub": "0.3.13",
    "tqdm": "4.67.1",
    "reportlab": "4.3.1",
    "nbformat": "5.10.4"
  },
  "system": "Linux-6.6.122+-x86_64-with-glibc2.39",
  "tensorflow_devices": [
    "PhysicalDevice(name='/physical_device:GPU:0', device_type='GPU')"
  ]
}''',
        'app/src/main/assets/models/expression/labels.txt': r'''Angry
Disgust
Fear
Happy
Sad
Surprise
Neutral
''',
        'app/src/main/assets/models/expression/model_settings.json': r'''{
  "course": "DLBAIPEAI",
  "task": "Task 2",
  "model_task": "expression",
  "architecture": "EfficientNetV2B0",
  "dataset": "ExpW",
  "class_names": [
    "Angry",
    "Disgust",
    "Fear",
    "Happy",
    "Sad",
    "Surprise",
    "Neutral"
  ],
  "input": {
    "shape": [
      1,
      224,
      224,
      3
    ],
    "dtype": "float32",
    "scale": 0.0,
    "zero_point": 0
  },
  "output": {
    "shape": [
      1,
      7
    ],
    "dtype": "float32",
    "scale": 0.0,
    "zero_point": 0
  },
  "chosen_format": "FP32",
  "source_run": "/content/project_edge_ai/results/expression/20260915_092434",
  "image_rule": "Crop face, convert to RGB, resize to 224x224 using bilinear interpolation.",
  "pixel_rule": "Float pixels 0-255. Scaling is inside the model. Do not divide by 255.",
  "integer_rule": "Use the saved tensor scale and zero point, with rounding and clipping.",
  "includes_face_detector": false,
  "measured_on_phone": false,
  "training_rerun": false,
  "conversion_validation_passed": true,
  "conversion_validation_images": 448,
  "conversion_limits": {
    "FP32": {
      "max_abs": 0.02,
      "mean_abs": 0.002,
      "agreement": 0.99,
      "balanced_drop": 0.01,
      "recall_drop": 0.03
    },
    "INT8": {
      "max_abs": 0.15,
      "mean_abs": 0.03,
      "agreement": 0.96,
      "balanced_drop": 0.03,
      "recall_drop": 0.05
    }
  },
  "tensorflow_recovery_version": "2.20.0",
  "training_seconds": null,
  "training_time_note": "Original timing variable was lost. No time is invented.",
  "holdout_scores": {
    "accuracy": 0.6577424844015882,
    "balanced_accuracy": 0.4607444225931667,
    "macro_f1": 0.4595262816082089
  },
  "holdout_scores_scope": "Original saved Keras results, not a new mobile holdout test.",
  "model_sha256": "04e2ac45ea9b630c9cde623585c2416013f5cc9cd60aaa3088f925c45df5059f"
}''',
        'app/src/main/assets/models/gender/labels.txt': r'''Female
Male
''',
        'app/src/main/assets/models/gender/model_settings.json': r'''{
  "course": "DLBAIPEAI",
  "task": "Task 2",
  "model_task": "gender",
  "architecture": "MobileNetV3Small",
  "seed": 42,
  "class_names": [
    "Female",
    "Male"
  ],
  "notebook_revision": "colab-gender-balanced-v1",
  "training_sampling": "exact half Female / half Male; shuffled cycles; training augmentation only",
  "training_class_weights": null,
  "samples_per_epoch": 78016,
  "checkpoint_rule": "highest validation balanced accuracy; validation loss breaks stage ties",
  "decision_rule": "argmax of Female/Male scores; no holdout-tuned threshold",
  "probability_note": "Balanced training changes the class prior; scores are model confidence values, not identity claims.",
  "gender_label_note": "The model predicts the two appearance labels supplied by FairFace.",
  "conversion_fp32_limits": {
    "max_abs": 0.01,
    "mean_abs": 0.001,
    "min_agreement": 0.99,
    "max_balanced_accuracy_drop": 0.01,
    "max_class_recall_drop": 0.02
  },
  "conversion_int8_limits": {
    "max_abs": 0.15,
    "mean_abs": 0.02,
    "min_agreement": 0.97,
    "max_balanced_accuracy_drop": 0.02,
    "max_class_recall_drop": 0.03
  },
  "dataset_handle": "mehmoodsheikh/fairface-dataset",
  "dataset_directory": "/content/project_edge_ai/datasets/kagglehub_cache/datasets/mehmoodsheikh/fairface-dataset/versions/1",
  "input": {
    "shape": [
      1,
      224,
      224,
      3
    ],
    "dtype": "float32",
    "scale": 0.0,
    "zero_point": 0
  },
  "output": {
    "shape": [
      1,
      2
    ],
    "dtype": "float32",
    "scale": 0.0,
    "zero_point": 0
  },
  "image_rule": "Crop face, convert to RGB, resize to 224x224 using bilinear interpolation.",
  "pixel_rule": "Float pixels 0-255. Scaling is already inside the model. Do not divide by 255.",
  "integer_rule": "For integer tensors use the saved scale and zero point, with rounding and clipping.",
  "includes_face_detector": false,
  "measured_on_phone": false,
  "split_sizes": {
    "train": 73555,
    "validation": 12981,
    "holdout": 10945
  },
  "chosen_stage": "fine",
  "chosen_format": "FP32",
  "training_seconds": 5816.838754453999,
  "gpu_name": "Tesla T4",
  "gpu_forward_backward_test_passed": true,
  "holdout_scores": {
    "accuracy": 0.8948378254910918,
    "balanced_accuracy": 0.8953790378510882,
    "macro_f1": 0.8946533578809162
  },
  "versions": {
    "tensorflow": "2.20.0",
    "kagglehub": "0.3.13",
    "tqdm": "4.67.1",
    "reportlab": "4.3.1",
    "nbformat": "5.10.4"
  },
  "system": "Linux-6.6.122+-x86_64-with-glibc2.39",
  "tensorflow_devices": [
    "PhysicalDevice(name='/physical_device:GPU:0', device_type='GPU')"
  ]
}''',
        'app/src/main/assets/models/manifest.json': r'''{
  "age": {
    "file": "age_android.tflite",
    "sha256": "573ed443697fdc6f2e5b30a12884b1de6f1d6992ff7c4c44fc70dda6653ee402",
    "bytes": 3735872,
    "format": "FP32",
    "class_names": [
      "Adult",
      "Elderly"
    ],
    "input_shape": [
      1,
      224,
      224,
      3
    ]
  },
  "gender": {
    "file": "gender_android.tflite",
    "sha256": "1ec406890ba521d617ca97848dcc91f2cf91f27b0fd7360ce788217214d85a4c",
    "bytes": 3731884,
    "format": "FP32",
    "class_names": [
      "Female",
      "Male"
    ],
    "input_shape": [
      1,
      224,
      224,
      3
    ]
  },
  "expression": {
    "file": "expression_android.tflite",
    "sha256": "04e2ac45ea9b630c9cde623585c2416013f5cc9cd60aaa3088f925c45df5059f",
    "bytes": 23450672,
    "format": "FP32",
    "class_names": [
      "Angry",
      "Disgust",
      "Fear",
      "Happy",
      "Sad",
      "Surprise",
      "Neutral"
    ],
    "input_shape": [
      1,
      224,
      224,
      3
    ]
  }
}''',
        'app/src/main/java/com/wpretorius/edgeface/MainActivity.kt': r'''package com.wpretorius.edgeface

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.wpretorius.edgeface.ui.EdgeApp

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent { EdgeApp() }
    }
}
''',
        'app/src/main/java/com/wpretorius/edgeface/core/ModelTypes.kt': r'''package com.wpretorius.edgeface.core

import java.nio.ByteBuffer
import java.nio.ByteOrder

// These are task names, not class indices. Indices always come from each model's metadata.
enum class Attribute(val folder: String, val title: String, val expectedLabels: Set<String>) {
    AGE("age", "Age group", setOf("Adult", "Elderly")),
    GENDER("gender", "Gender label", setOf("Female", "Male")),
    EXPRESSION("expression", "Expression", setOf("Angry", "Disgust", "Fear", "Happy", "Sad", "Surprise", "Neutral"))
}

enum class TensorKind(val bytes: Int) { FLOAT32(4), INT8(1), UINT8(1) }

// Shared, independently testable quantisation rules. Math.rint matches NumPy ties-to-even.
object TensorCodec {
    fun quantize(value: Float, kind: TensorKind, scale: Float, zeroPoint: Int): Int {
        require(value.isFinite()) { "Input contains a non-finite number." }
        require(kind != TensorKind.FLOAT32 && scale.isFinite() && scale > 0f)
        val low = if (kind == TensorKind.INT8) -128 else 0
        val high = if (kind == TensorKind.INT8) 127 else 255
        return Math.rint(value.toDouble() / scale + zeroPoint).coerceIn(low.toDouble(), high.toDouble()).toInt()
    }
    fun encode(values: FloatArray, kind: TensorKind, scale: Float, zeroPoint: Int): ByteBuffer {
        val buffer = ByteBuffer.allocateDirect(values.size * kind.bytes).order(ByteOrder.nativeOrder())
        values.forEach { value ->
            require(value.isFinite())
            if (kind == TensorKind.FLOAT32) buffer.putFloat(value)
            else buffer.put(quantize(value, kind, scale, zeroPoint).toByte())
        }
        buffer.rewind()
        return buffer
    }
    fun decode(buffer: ByteBuffer, size: Int, kind: TensorKind, scale: Float, zeroPoint: Int): FloatArray {
        buffer.rewind()
        require(kind == TensorKind.FLOAT32 || (scale.isFinite() && scale > 0))
        return FloatArray(size) {
            if (kind == TensorKind.FLOAT32) buffer.float
            else {
                val b = buffer.get()
                val q = if (kind == TensorKind.UINT8) b.toInt() and 255 else b.toInt()
                (q - zeroPoint) * scale
            }
        }
    }
    fun bestIndex(scores: FloatArray): Int {
        require(scores.isNotEmpty() && scores.all { it.isFinite() && it >= -0.03f && it <= 1.03f }) {
            "Model returned invalid scores. Check the model's input/output contract."
        }
        require(kotlin.math.abs(scores.sum() - 1f) <= 0.06f) { "Class scores do not sum close to 1." }
        return scores.indices.maxByOrNull { scores[it] }!!
    }
}

data class Prediction(
    val attribute: Attribute,
    val label: String,
    val score: Float,
    val scores: List<Pair<String, Float>>,
    val modelMs: Double,
    val preprocessingMs: Double,
    val modelHash: String,
    val format: String
)

data class ModelInfo(
    val attribute: Attribute, val labels: List<String>, val format: String,
    val bytes: Long, val hash: String, val holdoutAccuracy: Double?, val holdoutBalancedAccuracy: Double?
)
data class BenchmarkResult(val attribute: Attribute, val runs: Int, val medianMs: Double, val p95Ms: Double, val threads: Int, val hash: String)
object Numbers {
    fun percentile(values: List<Double>, fraction: Double): Double {
        require(values.isNotEmpty() && values.all { it.isFinite() && it >= 0 } && fraction in 0.0..1.0)
        val sorted = values.sorted(); val position = (sorted.size - 1) * fraction
        val low = position.toInt(); val high = kotlin.math.ceil(position).toInt()
        return sorted[low] + (sorted[high] - sorted[low]) * (position - low)
    }
}
''',
        'app/src/main/java/com/wpretorius/edgeface/core/PixelPreparation.kt': r'''package com.wpretorius.edgeface.core

import kotlin.math.abs
import kotlin.math.max

/** RGB resizing for the same bilinear rule used by Pillow in the notebooks. */
object PixelPreparation {
    const val POLICY = "mlkit_box_no_padding_pillow_bilinear_rgb_v1"
    private const val BITS = 22
    private data class Weights(val start: Int, val values: IntArray)

    private fun weights(input: Int, output: Int): List<Weights> {
        val scale = input.toDouble() / output
        val support = max(1.0, scale)
        return List(output) { position ->
            val center = (position + 0.5) * scale
            val first = (center - support + 0.5).toInt().coerceAtLeast(0)
            val end = (center + support + 0.5).toInt().coerceAtMost(input)
            val values = DoubleArray(end - first) { offset ->
                max(0.0, 1.0 - abs((offset + first - center + 0.5) / support))
            }
            val total = values.sum()
            require(total > 0)
            Weights(first, IntArray(values.size) { ((values[it] / total) * (1 shl BITS) + 0.5).toInt() })
        }
    }

    // Downscaling uses a wider filter. Plain Android bitmap scaling is not always the same.
    fun resizeRgb(source: IntArray, width: Int, height: Int, outWidth: Int = 224, outHeight: Int = 224): IntArray {
        require(width > 0 && height > 0 && outWidth > 0 && outHeight > 0)
        require(width.toLong() * height == source.size.toLong())
        require(outWidth.toLong() * max(height, outHeight) <= 16_000_000L)
        val horizontal = if (width == outWidth) source else {
            val wx = weights(width, outWidth)
            IntArray(outWidth * height).also { target ->
                for (y in 0 until height) for (x in 0 until outWidth) {
                    val w = wx[x]
                    var r = 1L shl (BITS - 1); var g = r; var b = r
                    for (j in w.values.indices) {
                        val p = source[y * width + w.start + j]; val k = w.values[j].toLong()
                        r += ((p shr 16) and 255) * k
                        g += ((p shr 8) and 255) * k
                        b += (p and 255) * k
                    }
                    target[y * outWidth + x] = pack(r, g, b)
                }
            }
        }
        if (height == outHeight) return horizontal.copyOf()
        val wy = weights(height, outHeight)
        return IntArray(outWidth * outHeight).also { target ->
            for (y in 0 until outHeight) for (x in 0 until outWidth) {
                val w = wy[y]
                var r = 1L shl (BITS - 1); var g = r; var b = r
                for (j in w.values.indices) {
                    val p = horizontal[(w.start + j) * outWidth + x]; val k = w.values[j].toLong()
                    r += ((p shr 16) and 255) * k
                    g += ((p shr 8) and 255) * k
                    b += (p and 255) * k
                }
                target[y * outWidth + x] = pack(r, g, b)
            }
        }
    }
    private fun pack(r: Long, g: Long, b: Long): Int =
        (255 shl 24) or ((r shr BITS).toInt().coerceIn(0, 255) shl 16) or
            ((g shr BITS).toInt().coerceIn(0, 255) shl 8) or (b shr BITS).toInt().coerceIn(0, 255)

    fun rgbValues(pixels: IntArray): FloatArray = FloatArray(pixels.size * 3).also { rgb ->
        pixels.forEachIndexed { i, p ->
            rgb[i * 3] = ((p shr 16) and 255).toFloat()
            rgb[i * 3 + 1] = ((p shr 8) and 255).toFloat()
            rgb[i * 3 + 2] = (p and 255).toFloat()
        }
    }
}
''',
        'app/src/main/java/com/wpretorius/edgeface/ml/FacePipeline.kt': r'''package com.wpretorius.edgeface.ml

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.SystemClock
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.face.FaceDetection
import com.google.mlkit.vision.face.FaceDetectorOptions
import com.wpretorius.edgeface.core.*
import java.io.Closeable
import java.util.concurrent.TimeUnit
import kotlin.math.abs

class CaptureProblem(val code: String, message: String) : Exception(message)
data class Analysis(
    val crop: Bitmap, val predictions: List<Prediction>, val detectionMs: Double, val totalMs: Double,
    val resizeMs: Double, val hints: List<String>
)

// The detector and the three models are only used on the analysis worker.
class FacePipeline(private val context: Context) : Closeable {
    private val detector = FaceDetection.getClient(FaceDetectorOptions.Builder()
        .setPerformanceMode(FaceDetectorOptions.PERFORMANCE_MODE_ACCURATE)
        .setLandmarkMode(FaceDetectorOptions.LANDMARK_MODE_NONE)
        .setClassificationMode(FaceDetectorOptions.CLASSIFICATION_MODE_NONE).build())
    private val models = mutableListOf<ModelRunner>()

    fun loadModels(): List<ModelInfo> {
        models.forEach { it.close() }; models.clear()
        try { Attribute.values().forEach { models += ModelRunner(context, it) } }
        catch (e: Throwable) { models.forEach { it.close() }; models.clear(); throw e }
        return models.map { it.info }
    }
    fun analyze(bitmap: Bitmap): Analysis {
        check(models.size == 3) { "All three bundled models must pass the startup test." }
        val started = SystemClock.elapsedRealtimeNanos()
        val faces = Tasks.await(detector.process(InputImage.fromBitmap(bitmap, 0)), 30, TimeUnit.SECONDS)
        val detectionMs = (SystemClock.elapsedRealtimeNanos() - started) / 1e6
        if (faces.isEmpty()) throw CaptureProblem("NO_FACE", "No face found. Use one clear, well-lit, front-facing adult face.")
        if (faces.size != 1) throw CaptureProblem("MULTIPLE_FACES", "Found ${faces.size} faces. Use one consenting adult at a time.")
        val face = faces.single()
        val box = Rect(face.boundingBox)
        if (!box.intersect(0, 0, bitmap.width, bitmap.height) || box.width() < 100 || box.height() < 100)
            throw CaptureProblem("FACE_TOO_SMALL", "The face is too small. Move closer and keep the whole face in view.")
        val resizeStart = SystemClock.elapsedRealtimeNanos()
        val original = IntArray(box.width() * box.height())
        bitmap.getPixels(original, 0, box.width(), box.left, box.top, box.width(), box.height())
        val prepared = PixelPreparation.resizeRgb(original, box.width(), box.height())
        val values = PixelPreparation.rgbValues(prepared)
        val inputBitmap = Bitmap.createBitmap(prepared, 224, 224, Bitmap.Config.ARGB_8888)
        val resizeMs = (SystemClock.elapsedRealtimeNanos() - resizeStart) / 1e6
        try {
            val predictions = models.map { it.predict(values) }
            val hints = mutableListOf<String>()
            if (abs(face.headEulerAngleY) > 25 || abs(face.headEulerAngleZ) > 20) hints += "The face is turned or tilted; estimates may be less reliable."
            val mean = values.average()
            if (mean < 45 || mean > 220) hints += "The face is very dark or bright; use even lighting for future captures."
            return Analysis(inputBitmap, predictions, detectionMs, (SystemClock.elapsedRealtimeNanos() - started) / 1e6, resizeMs, hints)
        } catch (e: Exception) { inputBitmap.recycle(); throw e }
    }
    fun benchmark(): List<BenchmarkResult> {
        check(models.size == 3)
        return models.map { it.benchmark() }
    }
    override fun close() { models.forEach { it.close() }; models.clear(); detector.close() }
}
''',
        'app/src/main/java/com/wpretorius/edgeface/ml/ModelRunner.kt': r'''package com.wpretorius.edgeface.ml

import android.content.Context
import android.os.SystemClock
import com.wpretorius.edgeface.core.*
import org.json.JSONObject
import org.tensorflow.lite.DataType
import org.tensorflow.lite.Interpreter
import org.tensorflow.lite.Tensor
import java.io.Closeable
import java.io.FileInputStream
import java.nio.channels.FileChannel
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import kotlin.math.abs

// Calls are kept on one worker. An Interpreter must not run two requests at once.
class ModelRunner(context: Context, private val attribute: Attribute) : Closeable {
    private val modelBuffer: ByteBuffer
    private val interpreter: Interpreter
    private val labels: List<String>
    private val input: Tensor
    private val output: Tensor
    private val inputBuffer: ByteBuffer
    private val outputBuffer: ByteBuffer
    val info: ModelInfo
    companion object { const val THREADS = 2 }

    init {
        val folder = "models/${attribute.folder}"
        fun text(name: String) = context.assets.open(name).bufferedReader().use { it.readText() }
        val settings = JSONObject(text("$folder/model_settings.json"))
        val declared = JSONObject(text("models/manifest.json")).getJSONObject(attribute.folder)
        require(settings.getString("model_task") == attribute.folder) { "Wrong model metadata in $folder." }
        val array = settings.getJSONArray("class_names")
        labels = (0 until array.length()).map { array.getString(it) }
        require(labels.size == attribute.expectedLabels.size && labels.toSet() == attribute.expectedLabels)
        require(text("$folder/labels.txt").lineSequence().filter { it.isNotBlank() }.map { it.trim() }.toList() == labels) {
            "Label files do not agree. Never reorder model outputs."
        }
        val manifestLabels = declared.getJSONArray("class_names")
        require(labels == (0 until manifestLabels.length()).map { manifestLabels.getString(it) })
        // Map the uncompressed asset instead of keeping a second heap copy of its weights.
        val mapped = context.assets.openFd("$folder/${attribute.folder}_android.tflite").use { descriptor ->
            val size = descriptor.declaredLength
            require(size == declared.getLong("bytes")) { "Bundled model size does not match." }
            FileInputStream(descriptor.fileDescriptor).use { stream ->
                stream.channel.map(FileChannel.MapMode.READ_ONLY, descriptor.startOffset, size)
                    .order(ByteOrder.nativeOrder()) to size
            }
        }
        modelBuffer = mapped.first
        val modelSize = mapped.second
        val digest = MessageDigest.getInstance("SHA-256").apply { update(modelBuffer.duplicate()) }
        val hash = digest.digest().joinToString("") { "%02x".format(it.toInt() and 255) }
        require(hash == declared.getString("sha256")) {
            "${attribute.title}: bundled model fingerprint failed. Reinstall the complete APK."
        }
        // CPU inference is the compatibility baseline; no phone GPU or Google Play runtime is required.
        interpreter = Interpreter(modelBuffer, Interpreter.Options().setNumThreads(THREADS))
        try {
            interpreter.allocateTensors()
            require(interpreter.inputTensorCount == 1 && interpreter.outputTensorCount == 1)
            input = interpreter.getInputTensor(0); output = interpreter.getOutputTensor(0)
            require(input.shape().contentEquals(intArrayOf(1, 224, 224, 3)))
            require(output.shape().contentEquals(intArrayOf(1, labels.size)))
            checkTensor(input, settings.getJSONObject("input"))
            checkTensor(output, settings.getJSONObject("output"))
            inputBuffer = ByteBuffer.allocateDirect(input.numBytes()).order(ByteOrder.nativeOrder())
            outputBuffer = ByteBuffer.allocateDirect(output.numBytes()).order(ByteOrder.nativeOrder())
            val scores = settings.optJSONObject("holdout_scores")
            info = ModelInfo(attribute, labels, settings.getString("chosen_format"), modelSize, hash,
                scores?.optDouble("accuracy")?.takeIf { it.isFinite() },
                scores?.optDouble("balanced_accuracy")?.takeIf { it.isFinite() })
            // A real invocation catches bad operators before a user takes a photograph.
            predict(FloatArray(224 * 224 * 3) { 127.5f })
        } catch (e: Exception) { interpreter.close(); throw e }
    }

    private fun kind(t: Tensor): TensorKind = when (t.dataType()) {
        DataType.FLOAT32 -> TensorKind.FLOAT32
        DataType.INT8 -> TensorKind.INT8
        DataType.UINT8 -> TensorKind.UINT8
        else -> error("Unsupported tensor type ${t.dataType()}")
    }
    private fun checkTensor(t: Tensor, declared: JSONObject) {
        val shape = declared.getJSONArray("shape")
        require(t.shape().contentEquals(IntArray(shape.length()) { shape.getInt(it) }))
        require(kind(t).name.lowercase() == declared.getString("dtype").lowercase())
        if (kind(t) != TensorKind.FLOAT32) {
            val actual = t.quantizationParams(); val expected = declared.getDouble("scale").toFloat()
            require(actual.scale > 0 && abs(actual.scale - expected) <= maxOf(1e-7f, abs(expected) * 1e-4f))
            require(actual.zeroPoint == declared.getInt("zero_point"))
        }
    }
    private fun fill(rgb: FloatArray) {
        require(rgb.size == 224 * 224 * 3 && rgb.all { it.isFinite() && it in 0f..255f })
        inputBuffer.rewind()
        val q = input.quantizationParams(); val type = kind(input)
        rgb.forEach { v ->
            if (type == TensorKind.FLOAT32) inputBuffer.putFloat(v)
            else inputBuffer.put(TensorCodec.quantize(v, type, q.scale, q.zeroPoint).toByte())
        }
        inputBuffer.rewind(); outputBuffer.rewind()
    }
    fun predict(rgb: FloatArray): Prediction {
        val start = SystemClock.elapsedRealtimeNanos()
        fill(rgb)
        val before = SystemClock.elapsedRealtimeNanos()
        interpreter.run(inputBuffer, outputBuffer)
        val after = SystemClock.elapsedRealtimeNanos()
        val q = output.quantizationParams()
        val scores = TensorCodec.decode(outputBuffer, labels.size, kind(output), q.scale, q.zeroPoint)
        val best = TensorCodec.bestIndex(scores)
        return Prediction(attribute, labels[best], scores[best], labels.zip(scores.toList()),
            (after - before) / 1e6, (before - start) / 1e6, info.hash, info.format)
    }
    fun benchmark(): BenchmarkResult {
        fill(FloatArray(224 * 224 * 3) { 127.5f })
        repeat(5) { inputBuffer.rewind(); outputBuffer.rewind(); interpreter.run(inputBuffer, outputBuffer) }
        val times = List(30) {
            inputBuffer.rewind(); outputBuffer.rewind()
            val before = SystemClock.elapsedRealtimeNanos()
            interpreter.run(inputBuffer, outputBuffer)
            (SystemClock.elapsedRealtimeNanos() - before) / 1e6
        }
        return BenchmarkResult(attribute, times.size, Numbers.percentile(times, 0.5), Numbers.percentile(times, 0.95), THREADS, info.hash)
    }
    override fun close() = interpreter.close()
}
fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes)
    .joinToString("") { "%02x".format(it.toInt() and 255) }
''',
        'app/src/main/java/com/wpretorius/edgeface/ml/PhotoInput.kt': r'''package com.wpretorius.edgeface.ml

import android.content.Context
import android.graphics.*
import android.net.Uri
import android.os.Build
import androidx.exifinterface.media.ExifInterface
import java.io.FileNotFoundException
import java.io.IOException
import kotlin.math.max
import kotlin.math.roundToInt

object PhotoInput {
    const val MAX_SIDE = 2048
    fun decode(context: Context, uri: Uri): Bitmap {
        try {
            require(uri.scheme == "content" || uri.scheme == "file") { "Select an image with Gallery or Files." }
            return decodeImage(context, uri)
        } catch (error: SecurityException) {
            throw CaptureProblem("IMAGE_ACCESS", "Access to this image has expired. Select it again with Gallery or Files.")
        } catch (error: FileNotFoundException) {
            throw CaptureProblem("IMAGE_MISSING", "This image is missing or not available offline. Download it to the phone and select it again.")
        } catch (error: IOException) {
            throw CaptureProblem("IMAGE_READ", "This image could not be read. Try a local JPEG or PNG. Online-only photos may need to be downloaded first.")
        } catch (error: IllegalArgumentException) {
            throw CaptureProblem("IMAGE_FORMAT", "This is not a supported image on this phone. Try a JPEG or PNG.")
        }
    }
    private fun decodeImage(context: Context, uri: Uri): Bitmap {
        if (Build.VERSION.SDK_INT >= 28) {
            // ImageDecoder reads EXIF rotation and mirror flags before returning the bitmap.
            val image = ImageDecoder.decodeBitmap(ImageDecoder.createSource(context.contentResolver, uri)) { decoder, info, _ ->
                require(info.size.width > 0 && info.size.height > 0) { "The image has no readable dimensions." }
                decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
                decoder.setTargetColorSpace(ColorSpace.get(ColorSpace.Named.SRGB))
                decoder.setOnPartialImageListener { false }
                val factor = (MAX_SIDE.toFloat() / max(info.size.width, info.size.height)).coerceAtMost(1f)
                decoder.setTargetSize(max(1, (info.size.width * factor).roundToInt()), max(1, (info.size.height * factor).roundToInt()))
            }
            return opaque(image)
        }
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        context.contentResolver.openInputStream(uri).use { requireNotNull(it); BitmapFactory.decodeStream(it, null, bounds) }
        require(bounds.outWidth > 0 && bounds.outHeight > 0) { "This file is not a readable image." }
        var sample = 1
        while (max(bounds.outWidth, bounds.outHeight) / sample > MAX_SIDE) sample *= 2
        val image = context.contentResolver.openInputStream(uri).use {
            requireNotNull(it)
            BitmapFactory.decodeStream(it, null, BitmapFactory.Options().apply {
                inSampleSize = sample; inPreferredConfig = Bitmap.Config.ARGB_8888
                inPreferredColorSpace = ColorSpace.get(ColorSpace.Named.SRGB)
            })
        } ?: error("The image could not be decoded.")
        val orientation = context.contentResolver.openInputStream(uri).use {
            requireNotNull(it)
            ExifInterface(it).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
        }
        val matrix = Matrix()
        when (orientation) {
            ExifInterface.ORIENTATION_FLIP_HORIZONTAL -> matrix.setScale(-1f, 1f)
            ExifInterface.ORIENTATION_ROTATE_180 -> matrix.setRotate(180f)
            ExifInterface.ORIENTATION_FLIP_VERTICAL -> matrix.setScale(1f, -1f)
            ExifInterface.ORIENTATION_TRANSPOSE -> { matrix.setRotate(90f); matrix.postScale(-1f, 1f) }
            ExifInterface.ORIENTATION_ROTATE_90 -> matrix.setRotate(90f)
            ExifInterface.ORIENTATION_TRANSVERSE -> { matrix.setRotate(-90f); matrix.postScale(-1f, 1f) }
            ExifInterface.ORIENTATION_ROTATE_270 -> matrix.setRotate(-90f)
        }
        if (matrix.isIdentity) return opaque(image)
        val rotated = Bitmap.createBitmap(image, 0, 0, image.width, image.height, matrix, true)
        if (rotated !== image) image.recycle()
        return opaque(rotated)
    }
    private fun opaque(image: Bitmap): Bitmap {
        if (!image.hasAlpha() && image.config == Bitmap.Config.ARGB_8888) return image
        val result = Bitmap.createBitmap(image.width, image.height, Bitmap.Config.ARGB_8888)
        Canvas(result).apply { drawColor(Color.WHITE); drawBitmap(image, 0f, 0f, null) }
        image.recycle()
        return result
    }
}
''',
        'app/src/main/java/com/wpretorius/edgeface/ui/CameraScreen.kt': r'''package com.wpretorius.edgeface.ui

import android.Manifest
import android.content.pm.PackageManager
import android.content.Context
import android.net.Uri
import android.util.Size
import android.view.Surface
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.lifecycle.compose.LocalLifecycleOwner
import java.io.File

// CameraX handles the camera differences for us. A system-camera fallback is also available.
@Composable
fun CameraScreen(onClose: () -> Unit, onSystemCamera: () -> Unit, onCaptured: (Uri, String, String) -> Unit) {
    val context = LocalContext.current
    val lifecycle = LocalLifecycleOwner.current
    val previewView = remember { PreviewView(context).apply {
        implementationMode = PreviewView.ImplementationMode.COMPATIBLE
        scaleType = PreviewView.ScaleType.FIT_CENTER
    } }
    var front by rememberSaveable { mutableStateOf(false) }
    var camera by remember { mutableStateOf<Camera?>(null) }
    var capture by remember { mutableStateOf<ImageCapture?>(null) }
    var canSwitch by remember { mutableStateOf(false) }
    var actualFront by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }
    var flash by remember { mutableStateOf(false) }
    var zoom by remember { mutableFloatStateOf(0f) }
    val executor = remember(context) { ContextCompat.getMainExecutor(context) }

    DisposableEffect(lifecycle, front) {
        var active = true
        var provider: ProcessCameraProvider? = null
        var boundPreview: Preview? = null
        var boundCapture: ImageCapture? = null
        capture = null; camera = null; message = null; zoom = 0f; flash = false
        val future = ProcessCameraProvider.getInstance(context)
        future.addListener({
            if (active) {
                try {
                    if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                        throw SecurityException("Camera permission is required.")
                    }
                    val p = future.get(); provider = p
                    val backAvailable = p.hasCamera(CameraSelector.DEFAULT_BACK_CAMERA)
                    val frontAvailable = p.hasCamera(CameraSelector.DEFAULT_FRONT_CAMERA)
                    canSwitch = backAvailable && frontAvailable
                    check(backAvailable || frontAvailable) { "No supported camera was found. Choose a photo instead." }
                    val useFront = if (front) frontAvailable else !backAvailable
                    actualFront = useFront
                    val selector = if (useFront) CameraSelector.DEFAULT_FRONT_CAMERA else CameraSelector.DEFAULT_BACK_CAMERA
                    val view = Preview.Builder().build()
                    val still = ImageCapture.Builder()
                        .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
                        .setJpegQuality(95)
                        .setResolutionSelector(ResolutionSelector.Builder().setResolutionStrategy(
                            ResolutionStrategy(Size(1280, 960), ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER)).build())
                        .setTargetRotation(previewView.display?.rotation ?: Surface.ROTATION_0)
                        .build()
                    view.setSurfaceProvider(previewView.surfaceProvider)
                    boundPreview = view; boundCapture = still
                    camera = p.bindToLifecycle(lifecycle, selector, view, still)
                    capture = still
                } catch (e: Exception) { message = "Camera could not start: ${e.message}. Try the system camera or a gallery photo." }
            }
        }, executor)
        onDispose {
            active = false
            val useCases = listOfNotNull(boundPreview, boundCapture).toTypedArray()
            if (useCases.isNotEmpty()) provider?.unbind(*useCases)
        }
    }
    Dialog(onDismissRequest = { if (!busy) onClose() }, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxSize(), color = Color(0xFF102321)) {
            Column(Modifier.fillMaxSize().systemBarsPadding().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    TextButton(onClick = onClose, enabled = !busy) { Text("Cancel", color = Color.White) }
                    TextButton(onClick = onSystemCamera, enabled = !busy) { Text("System camera", color = Color.White) }
                }
                Text("One face, even light", style = MaterialTheme.typography.titleLarge, color = Color.White)
                Text("Keep the whole face in view. This app analyzes a still photograph, not live video.", color = Color(0xFFD5E8E3))
                Box(Modifier.weight(1f).fillMaxWidth().background(Color.Black), contentAlignment = Alignment.Center) {
                    AndroidView(factory = { previewView }, modifier = Modifier.fillMaxSize())
                    if (capture == null && message == null) CircularProgressIndicator()
                }
                message?.let { Text(it, color = Color(0xFFFFDAD6)) }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { front = !front }, enabled = canSwitch && !busy, modifier = Modifier.weight(1f)) { Text("Switch camera", color = Color.White) }
                    OutlinedButton(onClick = {
                        flash = !flash; capture?.flashMode = if (flash) ImageCapture.FLASH_MODE_ON else ImageCapture.FLASH_MODE_OFF
                    }, enabled = camera?.cameraInfo?.hasFlashUnit() == true && !busy, modifier = Modifier.weight(1f)) {
                        Text(if (flash) "Flash on" else "Flash off", color = Color.White)
                    }
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Zoom", color = Color.White)
                    Slider(value = zoom, onValueChange = { zoom = it; camera?.cameraControl?.setLinearZoom(it) },
                        enabled = camera?.cameraInfo?.zoomState?.value?.let { it.maxZoomRatio > it.minZoomRatio } == true && !busy,
                        modifier = Modifier.weight(1f).padding(start = 12.dp))
                }
                Button(onClick = {
                    val still = capture ?: return@Button
                    busy = true; message = null
                    var file: File? = null
                    try {
                        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                            throw SecurityException("Camera permission was revoked.")
                        }
                        val target = newCaptureFile(context)
                        file = target
                        still.targetRotation = previewView.display?.rotation ?: Surface.ROTATION_0
                        still.takePicture(ImageCapture.OutputFileOptions.Builder(target).build(), executor, object : ImageCapture.OnImageSavedCallback {
                            override fun onImageSaved(results: ImageCapture.OutputFileResults) {
                                busy = false
                                val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", target)
                                onCaptured(uri, target.absolutePath, if (actualFront) "camerax_front" else "camerax_back")
                            }
                            override fun onError(error: ImageCaptureException) {
                                busy = false; target.delete(); message = "No photograph was saved: ${error.message}"
                            }
                        })
                    } catch (e: Exception) { busy = false; file?.delete(); message = "Camera error: ${e.message}" }
                }, enabled = capture != null && !busy, modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp)) {
                    Text(if (busy) "Saving photo…" else "Take photo")
                }
            }
        }
    }
}
fun newCaptureFile(context: Context): File {
    val directory = File(context.cacheDir, "captures").apply { check(mkdirs() || isDirectory) }
    // Remove temporary captures left over from an earlier use of the camera.
    directory.listFiles()?.filter { System.currentTimeMillis() - it.lastModified() > 24 * 60 * 60 * 1000L }?.forEach { it.delete() }
    return File.createTempFile("capture_", ".jpg", directory)
}
''',
        'app/src/main/java/com/wpretorius/edgeface/ui/EdgeApp.kt': r'''package com.wpretorius.edgeface.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.wpretorius.edgeface.BuildConfig
import com.wpretorius.edgeface.core.*
import java.io.File
import java.util.Locale

private val light = lightColorScheme(primary = Color(0xFF12695E), primaryContainer = Color(0xFFD0EDE3),
    background = Color(0xFFF3F7F5), surface = Color.White, onSurface = Color(0xFF172F2A))
private val dark = darkColorScheme(primary = Color(0xFF8CD5C3), primaryContainer = Color(0xFF174E43),
    background = Color(0xFF0E1C19), surface = Color(0xFF192A25))
private fun Double.ms(): String = String.format(Locale.US, "%.1f ms", this)
private fun percent(v: Double?): String = v?.let { String.format(Locale.US, "%.1f%%", it * 100) } ?: "Not measured"

@Composable
fun EdgeApp(model: EdgeViewModel = viewModel()) {
    val state by model.state.collectAsStateWithLifecycle()
    val context = LocalContext.current
    var page by rememberSaveable { mutableIntStateOf(0) }
    var consent by rememberSaveable { mutableStateOf(false) }
    var cameraVisible by rememberSaveable { mutableStateOf(false) }
    var pendingPath by rememberSaveable { mutableStateOf<String?>(null) }
    var externalBusy by rememberSaveable { mutableStateOf(false) }
    // Keep a selected photo until the models are ready, even after screen rotation.
    var pendingImageUri by rememberSaveable { mutableStateOf<String?>(null) }
    var pendingImageSource by rememberSaveable { mutableStateOf(ImageSource.GALLERY) }
    var pendingCapturePath by rememberSaveable { mutableStateOf<String?>(null) }
    val ready = state.models.size == 3 && !state.loading && !state.busy && !externalBusy && pendingImageUri == null

    fun acceptImage(uri: Uri?, path: String?, source: String) {
        cameraVisible = false
        externalBusy = false
        if (uri == null) {
            model.discardCapture(path)
            model.message("No image selected. The previous result has not been changed.")
            return
        }
        if (!consent) {
            model.discardCapture(path)
            model.message("Confirm permission to use the photo, then select it again.")
            return
        }
        page = 0
        pendingImageSource = source
        pendingCapturePath = path
        pendingImageUri = uri.toString()
    }
    val externalCamera = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { success ->
        val path = pendingPath
        pendingPath = null
        if (success && path != null && File(path).length() > 0) {
            acceptImage(FileProvider.getUriForFile(context, "${context.packageName}.files", File(path)), path, "system_camera")
        } else acceptImage(null, path, "system_camera")
    }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted && consent) cameraVisible = true
        else model.message("Camera access was not enabled. You can enable it in App settings, or choose a saved photo.")
    }
    // The phone gives access only to the selected picture.
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        acceptImage(uri, null, ImageSource.GALLERY)
    }
    val imageFiles = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        acceptImage(uri, null, ImageSource.FILES)
    }
    LaunchedEffect(pendingImageUri, state.loading, state.busy, state.modelError) {
        val picked = pendingImageUri ?: return@LaunchedEffect
        if (state.loading || state.busy) return@LaunchedEffect
        val path = pendingCapturePath
        val source = pendingImageSource
        pendingImageUri = null
        pendingCapturePath = null
        if (!consent || state.modelError != null || state.models.size != 3) {
            model.discardCapture(path)
            model.message("Confirm permission and wait for model checks before choosing an image.")
        } else model.analyze(Uri.parse(picked), path, source)
    }
    // Export the speed report directly from Models. No photo is included.
    val exportCsv = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("text/csv")) { uri ->
        externalBusy = false
        if (uri != null) model.exportBenchmark(uri, csvOnly = true)
    }
    val exportJson = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("application/json")) { uri ->
        externalBusy = false
        if (uri != null) model.exportBenchmark(uri, csvOnly = false)
    }
    fun safelyLaunch(action: () -> Unit) {
        try { action() } catch (e: Exception) {
            externalBusy = false
            model.message("This device could not open that screen: ${e.message ?: e.javaClass.simpleName}")
        }
    }
    fun openLibrary(useFiles: Boolean) {
        if (!ready || !consent) return
        externalBusy = true
        try {
            if (useFiles) imageFiles.launch(arrayOf("image/*"))
            else picker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
        } catch (error: Exception) {
            if (!useFiles) {
                try {
                    imageFiles.launch(arrayOf("image/*"))
                    model.message("The gallery picker could not open. Choose the image in Files instead.")
                    return
                } catch (_: Exception) { /* Show the message below. */ }
            }
            externalBusy = false
            model.message("This phone could not open the image picker. Try the other image button or the camera.")
        }
    }
    fun startCamera() {
        if (!ready || !consent) return
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) cameraVisible = true
        else safelyLaunch { permission.launch(Manifest.permission.CAMERA) }
    }
    fun useSystemCamera() {
        cameraVisible = false
        try {
            val file = newCaptureFile(context)
            pendingPath = file.absolutePath
            externalBusy = true
            externalCamera.launch(FileProvider.getUriForFile(context, "${context.packageName}.files", file))
        } catch (e: Exception) {
            externalBusy = false
            model.discardCapture(pendingPath)
            pendingPath = null
            model.message("No system camera could be opened. Try the in-app camera or choose a photo. ${e.message}")
        }
    }

    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) dark else light) {
        Scaffold { insets ->
            Box(Modifier.fillMaxSize().padding(insets), contentAlignment = Alignment.TopCenter) {
                Column(Modifier.widthIn(max = 760.dp).fillMaxWidth().verticalScroll(rememberScrollState())
                    .padding(horizontal = 20.dp, vertical = 18.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                        Column(Modifier.weight(1f)) {
                            Text("EdgeFace", style = MaterialTheme.typography.headlineLarge, fontWeight = FontWeight.Bold)
                            Text("Three models. On your phone.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Surface(color = MaterialTheme.colorScheme.primaryContainer, shape = RoundedCornerShape(50)) {
                            Text("OFFLINE", Modifier.padding(horizontal = 12.dp, vertical = 8.dp), style = MaterialTheme.typography.labelLarge)
                        }
                    }
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        listOf("Analyze", "Models", "Help").forEachIndexed { index, title ->
                            FilterChip(selected = page == index, onClick = { page = index }, label = { Text(title, style = MaterialTheme.typography.labelLarge) })
                        }
                    }
                    if (state.loading || state.busy) {
                        LinearProgressIndicator(Modifier.fillMaxWidth())
                        Text(if (state.loading) "Checking model fingerprints, tensors and test predictions…" else "Working on the phone. Please wait…", style = MaterialTheme.typography.bodySmall)
                    }
                    state.modelError?.let { InfoCard("Model startup failed", "$it\nNo predictions will be shown until all three models pass. Reinstall the complete APK and report this message.") }
                    state.message?.let { InfoCard("Status", it) }
                    when (page) {
                        0 -> {
                            InfoCard("One photograph. Three estimates.", "Take or choose a clear photo of one adult aged 20+. Age group, the dataset gender label and visible expression are estimated locally. Nothing is sent to a server.")
                            ConsentRow(consent, { consent = it }, !state.busy && !externalBusy, "I have permission to use this person's photo. The subject is aged 20 or over.")
                            Button(onClick = { startCamera() }, enabled = ready && consent, modifier = Modifier.fillMaxWidth()) { Text("Take photo") }
                            OutlinedButton(onClick = { openLibrary(false) }, enabled = ready && consent, modifier = Modifier.fillMaxWidth()) { Text("Choose from gallery") }
                            OutlinedButton(onClick = { openLibrary(true) }, enabled = ready && consent, modifier = Modifier.fillMaxWidth()) { Text("Browse image files") }
                            AnalysisCard(state)
                            if (state.analysis != null) TextButton(onClick = { model.clearImage() }, enabled = !state.busy) { Text("Clear displayed photo and results") }
                            Text("Take screenshots after the results appear. A screenshot can identify a face, so keep it private and use it only with permission.", style = MaterialTheme.typography.bodySmall)
                        }
                        1 -> {
                            Attribute.values().forEach { a ->
                                val info = state.models.firstOrNull { it.attribute == a }
                                InfoCard(a.title, if (info == null) "Not ready." else
                                    "${info.labels.joinToString(" · ")}\n${info.format} · ${String.format(Locale.US, "%.2f", info.bytes / 1e6)} MB\n" +
                                    "Original Keras holdout accuracy: ${percent(info.holdoutAccuracy)}\nBalanced accuracy: ${percent(info.holdoutBalancedAccuracy)}\nNot phone accuracy.\nSHA-256: ${info.hash}")
                            }
                            InfoCard("Phone CPU benchmark", "Each model gets 5 warm-ups and 30 timed calls using a fixed grey image. Median and p95 measure model invocation only, not decoding, face detection or resizing. Two CPU threads are used. Keep other apps quiet and the phone cool.")
                            Button(onClick = { model.benchmark() }, enabled = ready, modifier = Modifier.fillMaxWidth()) { Text("Run phone benchmark") }
                            val report = state.benchmark
                            if (report != null) {
                                InfoCard("Saved benchmark", "${report.device}\nEdgeFace ${report.appVersion}\nMeasured at UTC: ${report.utc ?: "Not recorded in the older report"}")
                                report.results.forEach { r -> InfoCard(r.attribute.title + " · measured on this phone", "Median ${r.medianMs.ms()}\np95 ${r.p95Ms.ms()}\n${r.runs} calls · ${r.threads} CPU threads") }
                                OutlinedButton(onClick = { externalBusy = true; safelyLaunch { exportCsv.launch("edgeface_phone_benchmark.csv") } }, enabled = ready, modifier = Modifier.fillMaxWidth()) { Text("Export benchmark CSV") }
                                OutlinedButton(onClick = { externalBusy = true; safelyLaunch { exportJson.launch("edgeface_phone_benchmark.json") } }, enabled = ready, modifier = Modifier.fillMaxWidth()) { Text("Export benchmark JSON") }
                                Text("Exports contain the measured timings, device details and model fingerprints. No photographs are included.", style = MaterialTheme.typography.bodySmall)
                            }
                        }
                        2 -> {
                            InfoCard("Use and compatibility", "Designed for Android 8.0+ phones and tablets with the required native libraries. A camera is optional for installation. The in-app camera offers front/back switching, zoom and flash when supported; System camera, Choose from gallery and Browse image files are alternatives. Not every camera, image format or future device can be guaranteed.")
                            TextButton(onClick = { safelyLaunch { context.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}"))) } }) { Text("Open Android app permissions") }
                            InfoCard("Photo library and image files", "On Analyze, confirm permission to use the photo, then tap Choose from gallery or Browse image files. Select one clear face image and analysis starts automatically. JPEG and PNG are recommended; other formats depend on the phone. Online-only pictures may need to be downloaded by the photo provider first. EdgeFace itself sends no image to a server.")
                            InfoCard("Privacy", "No Internet permission or cloud inference. New analysis photos are not added to a photo history. Temporary camera files are deleted after analysis. The displayed face is cleared when you clear the result or the app process ends. The camera/gallery app and any screenshots you take have their own storage behaviour. Only benchmark measurements are kept privately by this version.")
                            InfoCard("Know the limits", "Age supports only Adult 20–59 and Elderly 60+. It cannot establish an exact age or whether a person is a minor. Female/Male are FairFace appearance labels, not gender identity. Expression labels do not establish internal feelings. Scores are not calibrated probabilities; Fear and Disgust were weak classes in the training evaluation.")
                            InfoCard("Image preparation", "The photo is oriented to its EXIF flags, converted to sRGB and limited to 2048 pixels on the long edge. ML Kit locates one face. Its clipped bounding box is resized to 224 × 224 with a Pillow-style bilinear filter. Pixels stay 0–255. The actual 224 × 224 input is shown. Detector crops can differ from the training crops; evaluate this on real device photos.")
                            InfoCard("Credits", "Training data: FairFace (Kärkkäinen and Joo, 2021) and ExpW (Zhang, Luo, Loy and Tang, 2018). Models: MobileNetV3 and EfficientNetV2. On-device inference: Google LiteRT. Face detection: Google ML Kit. Camera: AndroidX CameraX.")
                            InfoCard("Share the installable app", "The professor needs the signed APK, not Android Studio. Build with the Python script, test the APK on a physical phone, then share it privately. Keep the signing key private. Do not publish course task sheets, training photos or participant images.")
                            Text("W. Pretorius · DLBAIPEAI · Task 2\nEdgeFace ${BuildConfig.VERSION_NAME}", style = MaterialTheme.typography.bodySmall)
                        }
                    }
                    Text("Academic demonstration only. Do not use these estimates for medical, hiring, access or identity decisions.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(12.dp))
                }
            }
        }
        if (cameraVisible) CameraScreen(onClose = { cameraVisible = false }, onSystemCamera = { useSystemCamera() },
            onCaptured = { uri, path, source -> acceptImage(uri, path, source) })
    }
}

@Composable
private fun ConsentRow(checked: Boolean, change: (Boolean) -> Unit, enabled: Boolean, text: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Checkbox(checked = checked, onCheckedChange = change, enabled = enabled)
        Text(text, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
    }
}
@Composable
private fun InfoCard(title: String, text: String) {
    Card(Modifier.fillMaxWidth(), shape = RoundedCornerShape(18.dp), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Text(text, style = MaterialTheme.typography.bodyMedium)
        }
    }
}
@Composable
private fun AnalysisCard(state: UiState) {
    val result = state.analysis ?: return
    Card(Modifier.fillMaxWidth(), shape = RoundedCornerShape(18.dp), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("The exact model input", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            Image(result.crop.asImageBitmap(), contentDescription = "224 by 224 RGB face input", contentScale = ContentScale.Fit,
                modifier = Modifier.fillMaxWidth().height(220.dp).background(MaterialTheme.colorScheme.surfaceVariant))
            Text("${ImageSource.title(state.analysisSource.orEmpty())} · ${state.analysisUtc.orEmpty()}\nEdgeFace ${BuildConfig.VERSION_NAME}", style = MaterialTheme.typography.bodySmall)
            result.predictions.forEach { p ->
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Column(Modifier.weight(1f)) {
                        Text(p.attribute.title, style = MaterialTheme.typography.labelLarge)
                        Text(p.label, style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold)
                    }
                    Column(horizontalAlignment = Alignment.End) {
                        Text(percent(p.score.toDouble()), style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
                        Text("Model score", style = MaterialTheme.typography.labelSmall)
                        Text(p.modelMs.ms(), style = MaterialTheme.typography.labelSmall)
                    }
                }
                val ranked = p.scores.sortedByDescending { it.second }
                Text(ranked.take(3).joinToString(" · ") { "${it.first} ${percent(it.second.toDouble())}" }, style = MaterialTheme.typography.bodySmall)
                if (ranked.size > 1 && ranked[0].second - ranked[1].second < 0.15f) Text("Close scores: this estimate is uncertain.", style = MaterialTheme.typography.bodySmall)
                HorizontalDivider()
            }
            result.hints.forEach { Text(it, style = MaterialTheme.typography.bodySmall) }
            Text("Detection ${result.detectionMs.ms()} · Shared resize ${result.resizeMs.ms()}\nTotal analysis ${result.totalMs.ms()} (not camera shutter time)", style = MaterialTheme.typography.bodySmall)
        }
    }
}
''',
        'app/src/main/java/com/wpretorius/edgeface/ui/EdgeViewModel.kt': r'''package com.wpretorius.edgeface.ui

import android.app.Application
import android.net.Uri
import android.os.Build
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.wpretorius.edgeface.BuildConfig
import com.wpretorius.edgeface.core.*
import com.wpretorius.edgeface.data.BenchmarkStore
import com.wpretorius.edgeface.ml.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.io.File
import java.time.Instant
import java.util.concurrent.Executors

// Slow work runs away from the screen thread so the buttons do not freeze.
data class UiState(
    val loading: Boolean = true, val busy: Boolean = false,
    val models: List<ModelInfo> = emptyList(), val modelError: String? = null,
    val analysis: Analysis? = null, val message: String? = null,
    val analysisSource: String? = null, val analysisUtc: String? = null,
    val benchmark: BenchmarkReport? = null
)
class EdgeViewModel(application: Application) : AndroidViewModel(application) {
    private val worker = Executors.newSingleThreadExecutor().asCoroutineDispatcher()
    private val store = BenchmarkStore(application)
    private val pipeline = FacePipeline(application)
    private val mutable = MutableStateFlow(UiState())
    val state = mutable.asStateFlow()
    private val device = "${Build.MANUFACTURER} ${Build.MODEL}; Android ${Build.VERSION.RELEASE}; SDK ${Build.VERSION.SDK_INT}; ABI ${Build.SUPPORTED_ABIS.firstOrNull()}"

    init {
        viewModelScope.launch {
            val models = withContext(worker) { runCatching { pipeline.loadModels() } }
            val saved = withContext(worker) { runCatching { store.read() } }
            val infos = models.getOrDefault(emptyList())
            val record = saved.getOrNull()
            val sameModels = record?.matches(infos) == true
            mutable.value = UiState(loading = false, models = infos,
                modelError = models.exceptionOrNull()?.let { it.message ?: it.javaClass.simpleName },
                benchmark = record?.takeIf { sameModels },
                message = when {
                    saved.isFailure -> "The old benchmark could not be read. Run a new benchmark on Models."
                    record != null && !sameModels -> "The old benchmark belongs to different models. Run a new benchmark."
                    else -> null
                })
        }
    }
    fun message(text: String) { mutable.value = mutable.value.copy(message = text) }
    fun clearImage() {
        if (!mutable.value.busy) mutable.value = mutable.value.copy(analysis = null, analysisSource = null, analysisUtc = null, message = null)
    }

    // Delete only a temporary capture created inside this app's cache.
    fun discardCapture(path: String?) {
        if (path == null) return
        runCatching {
            val file = File(path).canonicalFile
            val folder = File(getApplication<Application>().cacheDir, "captures").canonicalFile
            if (file.parentFile == folder) file.delete()
        }
    }

    fun analyze(uri: Uri, cameraFile: String? = null, source: String = ImageSource.GALLERY) {
        if (mutable.value.busy || mutable.value.loading) { discardCapture(cameraFile); return }
        if (mutable.value.models.size != 3) { message("Model checks have not passed."); discardCapture(cameraFile); return }
        mutable.value = mutable.value.copy(busy = true, analysis = null, analysisSource = source, analysisUtc = null, message = null)
        viewModelScope.launch {
            val result = withContext(worker) {
                try {
                    val start = SystemClock.elapsedRealtimeNanos()
                    val bitmap = PhotoInput.decode(getApplication(), uri)
                    val decodeMs = (SystemClock.elapsedRealtimeNanos() - start) / 1e6
                    val value = pipeline.analyze(bitmap)
                    Result.success(value.copy(totalMs = value.totalMs + decodeMs))
                } catch (error: CancellationException) { throw error }
                catch (error: OutOfMemoryError) {
                    Result.failure(CaptureProblem("LOW_MEMORY", "Not enough memory. Close other apps and try a smaller photo."))
                } catch (error: Exception) { Result.failure<Analysis>(error) }
                finally { discardCapture(cameraFile) }
            }
            mutable.value = mutable.value.copy(busy = false, analysis = result.getOrNull(),
                analysisUtc = Instant.now().toString(),
                message = result.exceptionOrNull()?.let { it.message ?: it.javaClass.simpleName })
        }
    }
    fun benchmark() {
        if (mutable.value.busy || mutable.value.models.size != 3) return
        mutable.value = mutable.value.copy(busy = true, message = "Benchmark: 5 warm-ups and 30 timed calls per model.")
        viewModelScope.launch {
            var saveError: String? = null
            val result = withContext(worker) { runCatching {
                val report = BenchmarkReport(device, BuildConfig.VERSION_NAME, Instant.now().toString(), pipeline.benchmark())
                try { store.save(report) } catch (e: Exception) { saveError = e.message ?: e.javaClass.simpleName }
                report
            } }
            mutable.value = mutable.value.copy(busy = false, benchmark = result.getOrNull() ?: mutable.value.benchmark,
                message = when {
                    result.isFailure -> "Benchmark failed. Any previous result is unchanged. ${result.exceptionOrNull()?.message}"
                    saveError != null -> "Benchmark measured but could not be saved privately. Export it now. $saveError"
                    else -> "Phone CPU benchmark saved. Export it below. This is model time, not camera speed."
                })
        }
    }
    fun exportBenchmark(uri: Uri, csvOnly: Boolean) {
        if (mutable.value.busy) return
        val report = mutable.value.benchmark ?: run { message("Run the benchmark before exporting it."); return }
        mutable.value = mutable.value.copy(busy = true)
        viewModelScope.launch {
            val result = withContext(worker) { runCatching {
                val text = if (csvOnly) BenchmarkCsv.export(report) else store.asJson(report)
                getApplication<Application>().contentResolver.openOutputStream(uri, "wt").use { out ->
                    requireNotNull(out) { "Could not open the chosen destination." }
                    out.write(text.toByteArray(Charsets.UTF_8))
                }
            } }
            mutable.value = mutable.value.copy(busy = false,
                message = if (result.isSuccess) "Benchmark exported. No photographs were included." else "Export failed: ${result.exceptionOrNull()?.message}")
        }
    }
    override fun onCleared() {
        CoroutineScope(worker).launch { pipeline.close(); worker.close() }
        super.onCleared()
    }
}
''',
        'app/src/main/res/drawable/ic_app.xml': r'''<vector xmlns:android="http://schemas.android.com/apk/res/android" android:width="48dp" android:height="48dp" android:viewportWidth="48" android:viewportHeight="48">
    <path android:fillColor="#12352F" android:pathData="M0,0h48v48h-48z" />
    <path android:strokeColor="#B7E7D5" android:strokeWidth="2.4" android:fillColor="@android:color/transparent" android:pathData="M9,17v-7h7M32,10h7v7M39,31v7h-7M16,38h-7v-7M16,23a8,10 0,1 0,16 0a8,10 0,1 0,-16 0" />
</vector>
''',
        'app/src/main/res/values/themes.xml': r'''<resources>
    <style name="Theme.EdgeFace" parent="android:style/Theme.Material.Light.NoActionBar">
        <item name="android:fontFamily">sans</item>
        <item name="android:windowLightStatusBar">true</item>
        <item name="android:colorAccent">#176C63</item>
        <item name="android:windowActionModeOverlay">true</item>
    </style>
</resources>
''',
        'app/src/main/res/xml/data_extraction_rules.xml': r'''<?xml version="1.0" encoding="utf-8"?>
<data-extraction-rules>
    <cloud-backup>
        <exclude domain="root" path="." />
        <exclude domain="file" path="." />
        <exclude domain="database" path="." />
        <exclude domain="sharedpref" path="." />
        <exclude domain="external" path="." />
    </cloud-backup>
    <device-transfer>
        <exclude domain="root" path="." />
        <exclude domain="file" path="." />
        <exclude domain="database" path="." />
        <exclude domain="sharedpref" path="." />
        <exclude domain="external" path="." />
    </device-transfer>
</data-extraction-rules>
''',
        'app/src/main/res/xml/file_paths.xml': r'''<?xml version="1.0" encoding="utf-8"?>
<paths xmlns:android="http://schemas.android.com/apk/res/android">
    <cache-path name="captures" path="captures/" />
</paths>
''',
        'app/src/test/java/com/wpretorius/edgeface/core/CoreChecks.kt': r'''package com.wpretorius.edgeface.core

import java.security.MessageDigest
import kotlin.math.abs

// These tests do not need a phone. They test image preparation, model numbers and benchmark reports.
object CoreChecks {
    fun run(): Int {
        var passed = 0
        fun test(block: () -> Unit) { block(); passed++ }
        fun rejects(block: () -> Unit) { var thrown = false; try { block() } catch (_: IllegalArgumentException) { thrown = true }; check(thrown) }
        test { check(ImageSource.isCamera("camerax_front")) }
        test { check(ImageSource.isCamera("camerax_back")) }
        test { check(ImageSource.isCamera("system_camera")) }
        test { check(!ImageSource.isCamera(ImageSource.GALLERY)) }
        test { check(!ImageSource.isCamera(ImageSource.FILES)) }
        test { check(!ImageSource.isCamera("unknown")) }
        test { check(ImageSource.title(ImageSource.GALLERY) == "Gallery image") }
        test { check(ImageSource.title(ImageSource.FILES) == "Image file") }
        test { check(ImageSource.title("camerax_front") == "Front camera") }
        test { check(ImageSource.title("camerax_back") == "Back camera") }
        test { check(ImageSource.title("system_camera") == "System camera") }
        test { check(ImageSource.title("unknown") == "Image") }
        test { check(TensorCodec.quantize(0f, TensorKind.INT8, 1f, -128) == -128) }
        test { check(TensorCodec.quantize(255f, TensorKind.INT8, 1f, -128) == 127) }
        test { check(TensorCodec.quantize(2.5f, TensorKind.UINT8, 1f, 0) == 2) }
        test { check(TensorCodec.quantize(3.5f, TensorKind.UINT8, 1f, 0) == 4) }
        test { rejects { TensorCodec.quantize(Float.NaN, TensorKind.INT8, 1f, 0) } }
        test { rejects { TensorCodec.quantize(1f, TensorKind.INT8, 0f, 0) } }
        for (kind in TensorKind.values()) test {
            val input = floatArrayOf(0f, 127f, 255f)
            val zero = if (kind == TensorKind.INT8) -128 else 0
            val b = TensorCodec.encode(input, kind, 1f, zero)
            val output = TensorCodec.decode(b, input.size, kind, 1f, zero)
            check(output.contentEquals(input))
        }
        test { check(TensorCodec.bestIndex(floatArrayOf(.2f, .8f)) == 1) }
        test { rejects { TensorCodec.bestIndex(floatArrayOf(.1f, .1f)) } }
        test { rejects { TensorCodec.bestIndex(floatArrayOf(Float.NaN, 1f)) } }
        test { check(TensorCodec.bestIndex(floatArrayOf(.5f, .5f)) == 0) }
        test { check(abs(Numbers.percentile(listOf(3.0, 1.0, 2.0, 4.0), .5) - 2.5) < 1e-10) }
        test { check(Numbers.percentile(listOf(3.0), .95) == 3.0) }
        test { rejects { Numbers.percentile(emptyList(), .5) } }
        val results = Attribute.values().map { BenchmarkResult(it, 30, 2.5, 4.0, 2, "a".repeat(64)) }
        val report = BenchmarkReport("Test phone", "1.2.0", "2026-09-16T00:00:00Z", results)
        val models = results.map { ModelInfo(it.attribute, it.attribute.expectedLabels.toList(), "FP32", 100L, it.hash, null, null) }
        test { check(report.matches(models)) }
        test { check(report.matches(models.reversed())) }
        test { check(!report.matches(models.dropLast(1))) }
        test { check(!report.matches(listOf(models.first(), models.first(), models.last()))) }
        test { check(!report.matches(models.map { it.copy(hash = "b".repeat(64)) })) }
        test { check(report.copy(results = results.map { it.copy(hash = "A".repeat(64)) }).matches(models)) }
        test { check(report.copy(utc = null).utc == null) }
        test { rejects { report.copy(utc = "made-up time") } }
        test { rejects { report.copy(device = "") } }
        test { rejects { report.copy(appVersion = "  ") } }
        test { rejects { report.copy(results = emptyList()) } }
        test { rejects { report.copy(results = results.dropLast(1)) } }
        test { rejects { report.copy(results = listOf(results[0], results[0], results[2])) } }
        test { rejects { report.copy(results = results.map { it.copy(runs = 0) }) } }
        test { rejects { report.copy(results = results.map { it.copy(runs = 29) }) } }
        test { rejects { report.copy(results = results.map { it.copy(threads = 0) }) } }
        test { rejects { report.copy(results = results.map { it.copy(medianMs = Double.NaN) }) } }
        test { rejects { report.copy(results = results.map { it.copy(p95Ms = Double.POSITIVE_INFINITY) }) } }
        test { rejects { report.copy(results = results.map { it.copy(medianMs = -0.1) }) } }
        test { rejects { report.copy(results = results.map { it.copy(p95Ms = 2.4) }) } }
        test { rejects { report.copy(results = results.map { it.copy(hash = "a".repeat(63)) }) } }
        test { rejects { report.copy(results = results.map { it.copy(hash = "x".repeat(64)) }) } }
        test { check(BenchmarkCsv.field("=1+1").startsWith("\"'=")) }
        test { check(BenchmarkCsv.field(" \t@SUM(1)").startsWith("\"'")) }
        test { check(BenchmarkCsv.field("a\"b") == "\"a\"\"b\"") }
        test { check(BenchmarkCsv.field(null) == "\"\"") }
        test { check(BenchmarkCsv.field("a,b") == "\"a,b\"") }
        test { check(BenchmarkCsv.field("a\nb") == "\"a\nb\"") }
        test { check(BenchmarkCsv.export(report).lines().filter { it.isNotBlank() }.size == 4) }
        test { check(BenchmarkCsv.export(report).startsWith("device,app_version,measured_at_utc,model,")) }
        test { check(BenchmarkCsv.export(report).contains("\"expression\",\"30\",\"2\",\"5\",\"2.5\",\"4.0\"")) }
        test { check(BenchmarkCsv.export(report.copy(utc = null)).contains("\"1.2.0\",\"\",\"age\"")) }
        test { check(BenchmarkCsv.export(report).contains(BenchmarkCsv.SCOPE)) }
        test { check(BenchmarkCsv.export(report.copy(device = "=1+1")).contains("\"'=1+1\"")) }
        test { check(BenchmarkCsv.export(report.copy(results = results.reversed())) == BenchmarkCsv.export(report)) }
        test { check(PixelPreparation.rgbValues(intArrayOf(0xff123456.toInt())).contentEquals(floatArrayOf(18f,52f,86f))) }
        test { rejects { PixelPreparation.resizeRgb(intArrayOf(1), 3, 2) } }
        // These expected hashes were made with Pillow, not with the app's resizer.
        val sizes = listOf(
            intArrayOf(1, 1, 7, 5),
            intArrayOf(2, 3, 224, 224),
            intArrayOf(100, 101, 224, 224),
            intArrayOf(224, 224, 224, 224),
            intArrayOf(275, 371, 224, 224),
            intArrayOf(760, 681, 224, 224),
            intArrayOf(2048, 1024, 224, 224),
            intArrayOf(224, 57, 224, 224),
            intArrayOf(57, 224, 224, 224),
            intArrayOf(411, 611, 73, 57),
            intArrayOf(513, 257, 257, 513),
            intArrayOf(17, 31, 31, 17)
        )
        val expectedHashes = listOf(
            "d3110206bde6f1e2a0ecf6d8859e0cbc52b61fc637558b229af005f5f564f91c",
            "5896804757fc655051d34221770c5ebdc13d849fcef875c7478a9f42b49ccef2",
            "24c9766dafdea2896501e83d6cba81a81126c1ec65c2c0ba8a83d0116a055ec6",
            "e850bdced6215f233f7eb30f333a169cdd84bd3827a00ac1d5468d5f6402605b",
            "3a5b5d954c96ce2b1e4bf1b3b3270e2cd4f9d105c7d64cb8c541d6116cd55d2b",
            "3713a89a9df6d5b0091d45ddd9ab7eb8a3e6ab7c0dcb851af31eb86c61b824c2",
            "941492519e02eed99e6995cf9258327f6b010f96d60b18d15f42efe7aa729c42",
            "41e84c026d267579161862884c296a2b921fd1f789d9c9b52b5a5e2dfab00e9f",
            "1e8ce5c7fde7f02015c87d4b5682595b8f380cb535c343064b01f1166f00d794",
            "24b07e22d97c23b66a223c6f0aef085c7d07a4416762ae0df3b1e5faadca93a9",
            "466e33e3e1e5c99f8450aff4ec5656efda0791c2a34b6997b7276fab40cd549a",
            "c71d3b9f7dc540a9721af63c0f68d0fea0e31c986ace0330ed841a192b09fd3a"
        )
        for (i in sizes.indices) test {
            val (w, h, ow, oh) = sizes[i]
            var state = 42L + i
            // The same short rule creates the test picture on every computer.
            val input = IntArray(w * h) {
                state = (state * 1664525L + 1013904223L) and 0xffffffffL
                0xff000000.toInt() or ((state ushr 8).toInt() and 0x00ffffff)
            }
            val resized = PixelPreparation.resizeRgb(input, w, h, ow, oh)
            val rgb = ByteArray(resized.size * 3)
            resized.forEachIndexed { j, p ->
                rgb[j * 3] = (p ushr 16).toByte()
                rgb[j * 3 + 1] = (p ushr 8).toByte()
                rgb[j * 3 + 2] = p.toByte()
            }
            val actual = MessageDigest.getInstance("SHA-256").digest(rgb)
                .joinToString("") { "%02x".format(it.toInt() and 255) }
            check(actual == expectedHashes[i]) { "Pillow reference case $i did not match." }
        }
        return passed
    }
}
''',
        'app/src/test/java/com/wpretorius/edgeface/core/CoreLogicTest.kt': r'''package com.wpretorius.edgeface.core

import org.junit.Test

class CoreLogicTest {
    @Test fun coreAndPillowReferenceChecks() {
        check(CoreChecks.run() == 77)
    }
}
''',
        'app/src/test/resources/resize_fixtures/README.json': r'''{
  "method": "Test input is generated in CoreChecks.kt. No external binary test files are needed.",
  "input_rule": "For each pixel: state = (1664525 * state + 1013904223) mod 2^32. RGB = bits 31..8.",
  "reference": "Pillow Image.Resampling.BILINEAR on RGB images; hash RGB output bytes in row order.",
  "pillow_version": "12.3.0",
  "cases": [
    {
      "shape": [
        1,
        1,
        7,
        5
      ],
      "seed": 42,
      "rgb_sha256": "d3110206bde6f1e2a0ecf6d8859e0cbc52b61fc637558b229af005f5f564f91c"
    },
    {
      "shape": [
        2,
        3,
        224,
        224
      ],
      "seed": 43,
      "rgb_sha256": "5896804757fc655051d34221770c5ebdc13d849fcef875c7478a9f42b49ccef2"
    },
    {
      "shape": [
        100,
        101,
        224,
        224
      ],
      "seed": 44,
      "rgb_sha256": "24c9766dafdea2896501e83d6cba81a81126c1ec65c2c0ba8a83d0116a055ec6"
    },
    {
      "shape": [
        224,
        224,
        224,
        224
      ],
      "seed": 45,
      "rgb_sha256": "e850bdced6215f233f7eb30f333a169cdd84bd3827a00ac1d5468d5f6402605b"
    },
    {
      "shape": [
        275,
        371,
        224,
        224
      ],
      "seed": 46,
      "rgb_sha256": "3a5b5d954c96ce2b1e4bf1b3b3270e2cd4f9d105c7d64cb8c541d6116cd55d2b"
    },
    {
      "shape": [
        760,
        681,
        224,
        224
      ],
      "seed": 47,
      "rgb_sha256": "3713a89a9df6d5b0091d45ddd9ab7eb8a3e6ab7c0dcb851af31eb86c61b824c2"
    },
    {
      "shape": [
        2048,
        1024,
        224,
        224
      ],
      "seed": 48,
      "rgb_sha256": "941492519e02eed99e6995cf9258327f6b010f96d60b18d15f42efe7aa729c42"
    },
    {
      "shape": [
        224,
        57,
        224,
        224
      ],
      "seed": 49,
      "rgb_sha256": "41e84c026d267579161862884c296a2b921fd1f789d9c9b52b5a5e2dfab00e9f"
    },
    {
      "shape": [
        57,
        224,
        224,
        224
      ],
      "seed": 50,
      "rgb_sha256": "1e8ce5c7fde7f02015c87d4b5682595b8f380cb535c343064b01f1166f00d794"
    },
    {
      "shape": [
        411,
        611,
        73,
        57
      ],
      "seed": 51,
      "rgb_sha256": "24b07e22d97c23b66a223c6f0aef085c7d07a4416762ae0df3b1e5faadca93a9"
    },
    {
      "shape": [
        513,
        257,
        257,
        513
      ],
      "seed": 52,
      "rgb_sha256": "466e33e3e1e5c99f8450aff4ec5656efda0791c2a34b6997b7276fab40cd549a"
    },
    {
      "shape": [
        17,
        31,
        31,
        17
      ],
      "seed": 53,
      "rgb_sha256": "c71d3b9f7dc540a9721af63c0f68d0fea0e31c986ace0330ed841a192b09fd3a"
    }
  ]
}
''',
        'build.gradle.kts': r'''plugins {
    id("com.android.application") version "8.13.2" apply false
    id("org.jetbrains.kotlin.android") version "2.3.10" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.3.10" apply false
}
''',
        'docs/MODEL_INTEGRATION.md': r'''# Integrated trained models

These files were taken from the user's supplied archives, not retrained or substituted. The settings JSON and labels.txt files are included unchanged beside each model. `assets/models/manifest.json` records the exact model hashes and class order.

| Model | Supplied archive | Android export | Bytes | Format |
|---|---|---|---:|---|
| Age | age_results_20260915_044643.zip | age_android.tflite | 3,735,872 | FP32 |
| Gender | gender_results_20260915_030921.zip | gender_android.tflite | 3,731,884 | FP32 |
| Expression | expression_android_bundle_20260915_142717_617660.zip | expression_android.tflite | 23,450,672 | FP32 |

All three inputs are FLOAT32 `[1,224,224,3]`, RGB values **0–255**. Input scaling is already inside the network. Outputs are FLOAT32 `[1,2]`, `[1,2]` and `[1,7]` respectively.

Exact orders:

- Age: Adult, Elderly.
- Gender: Female, Male.
- Expression: Angry, Disgust, Fear, Happy, Sad, Surprise, Neutral.

The user-supplied expression validation records approve FP32 and reject INT8. The rejected INT8 file is **not** bundled as an alternative. Age and gender also selected FP32. A smaller file is not automatically a better deployment choice.

## Preserved original Keras holdout measurements

| Model | Accuracy | Balanced accuracy | Macro F1 |
|---|---:|---:|---:|
| Age | 0.9118685 | 0.8663997 | 0.7244617 |
| Gender | 0.8948378 | 0.8953790 | 0.8946534 |
| Expression | 0.6577425 | 0.4607444 | 0.4595263 |

These are **original training-computer Keras holdout** results, not measurements from this Android app. The age majority-class accuracy in its imbalanced holdout can exceed the balanced model's overall accuracy; do not suppress that baseline. The expression model has weak minority-class recall. Integration does not repair those statistical limitations.

The `docs/model_evidence` CSVs are copied from the supplied archives. Original training sets, notebook photos, optimizer checkpoints and unrelated recovery files are not in the app. No trained model accuracy has been recomputed in the preparation environment because a TFLite runtime was not installed there.

## Input contract and pipeline

1. Request one user-selected photograph or a still camera capture.
2. Decode to software sRGB; honor orientation and mirror flags; cap long edge at 2048 to bound memory.
3. Detect exactly one face with bundled ML Kit. Clip its bounding rectangle to the decoded bitmap. Reject rectangles under 100 pixels per side.
4. Resize the RGB crop with the tested Pillow-style separable bilinear filter. Do not apply an additional division by 255.
5. Use the same prepared 224 × 224 RGB values for all three model invocations.
6. Read outputs in the saved order. Score magnitude is not a calibrated confidence guarantee. Do not force Happy/Sad for the expression model.

The shared resize policy is versioned as `mlkit_box_no_padding_pillow_bilinear_rgb_v1`. No face-recognition identity matching, stored names, gender-identity inference or emotional-state diagnosis is implemented.

## Source map

- `ui/EdgeApp.kt`: screens, consent, capture and export actions.
- `ui/CameraScreen.kt`: camera preview, still capture, front/back, zoom and flash.
- `ui/EdgeViewModel.kt`: one analysis worker, state, model startup, benchmark persistence and export.
- `ml/PhotoInput.kt`: bounded decoding, orientation, sRGB and alpha handling.
- `ml/FacePipeline.kt`: detector, fixed crop policy, shared input and three model calls.
- `ml/ModelRunner.kt`: original model/label/shape/hash checks and LiteRT invocation.
- `core/PixelPreparation.kt`: independent bilinear RGB resizing and RGB extraction.
- `core/Benchmark.kt`: measured benchmark checks and spreadsheet-safe CSV.
- `data/BenchmarkStore.kt`: atomic benchmark storage and JSON export. No photo history.
- `app/src/test`: pure Kotlin checks and generated synthetic images compared with fixed Pillow reference hashes.
- `app/src/androidTest`: real Android runtime tests for model invocation, permissions, EXIF and storage.
''',
        'docs/model_contract_audit.json': r'''{
  "age": {
    "model": {
      "file": "age_android.tflite",
      "sha256": "573ed443697fdc6f2e5b30a12884b1de6f1d6992ff7c4c44fc70dda6653ee402",
      "bytes": 3735872,
      "format": "FP32",
      "class_names": [
        "Adult",
        "Elderly"
      ],
      "input_shape": [
        1,
        224,
        224,
        3
      ]
    },
    "flatbuffer": {
      "input": {
        "name": "serving_default_face_rgb:0",
        "shape": [
          1,
          224,
          224,
          3
        ],
        "dtype_enum": 0,
        "scales": [],
        "zeros": []
      },
      "output": {
        "name": "StatefulPartitionedCall_1:0",
        "shape": [
          1,
          2
        ],
        "dtype_enum": 0,
        "scales": [],
        "zeros": []
      },
      "operators": [
        {
          "builtin": 18,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 0,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 3,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 117,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 4,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 40,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 9,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 25,
          "version": 1,
          "custom": null
        }
      ],
      "tensor_count": 219,
      "all_tensor_types": [
        0,
        2
      ]
    }
  },
  "gender": {
    "model": {
      "file": "gender_android.tflite",
      "sha256": "1ec406890ba521d617ca97848dcc91f2cf91f27b0fd7360ce788217214d85a4c",
      "bytes": 3731884,
      "format": "FP32",
      "class_names": [
        "Female",
        "Male"
      ],
      "input_shape": [
        1,
        224,
        224,
        3
      ]
    },
    "flatbuffer": {
      "input": {
        "name": "serving_default_face_rgb:0",
        "shape": [
          1,
          224,
          224,
          3
        ],
        "dtype_enum": 0,
        "scales": [],
        "zeros": []
      },
      "output": {
        "name": "StatefulPartitionedCall_1:0",
        "shape": [
          1,
          2
        ],
        "dtype_enum": 0,
        "scales": [],
        "zeros": []
      },
      "operators": [
        {
          "builtin": 18,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 0,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 3,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 117,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 4,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 40,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 9,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 25,
          "version": 1,
          "custom": null
        }
      ],
      "tensor_count": 219,
      "all_tensor_types": [
        0,
        2
      ]
    }
  },
  "expression": {
    "model": {
      "file": "expression_android.tflite",
      "sha256": "04e2ac45ea9b630c9cde623585c2416013f5cc9cd60aaa3088f925c45df5059f",
      "bytes": 23450672,
      "format": "FP32",
      "class_names": [
        "Angry",
        "Disgust",
        "Fear",
        "Happy",
        "Sad",
        "Surprise",
        "Neutral"
      ],
      "input_shape": [
        1,
        224,
        224,
        3
      ]
    },
    "flatbuffer": {
      "input": {
        "name": "serving_default_face_rgb:0",
        "shape": [
          1,
          224,
          224,
          3
        ],
        "dtype_enum": 0,
        "scales": [],
        "zeros": []
      },
      "output": {
        "name": "StatefulPartitionedCall_1:0",
        "shape": [
          1,
          7
        ],
        "dtype_enum": 0,
        "scales": [],
        "zeros": []
      },
      "operators": [
        {
          "builtin": 18,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 41,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 3,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 14,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 0,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 4,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 40,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 77,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 45,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 83,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 22,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 9,
          "version": 1,
          "custom": null
        },
        {
          "builtin": 25,
          "version": 1,
          "custom": null
        }
      ],
      "tensor_count": 531,
      "all_tensor_types": [
        0,
        2
      ]
    }
  }
}''',
        'docs/model_evidence/age/accuracy_by_original_age_range.csv': r'''fairface_age_range,project_class,images,accuracy
20-29,Adult,3299,0.9890876023037284
30-39,Adult,2329,0.9695148132245599
40-49,Adult,1352,0.8550295857988166
50-59,Adult,796,0.5728643216080402
60-69,Elderly,321,0.7757009345794392
more than 70,Elderly,118,0.923728813559322
''',
        'docs/model_evidence/age/classification_report.csv': r''',precision,recall,f1-score,support
Adult,0.9887718325478236,0.917309670781893,0.9517011340893929,7776.0
Elderly,0.35764235764235763,0.8154897494305239,0.49722222222222223,439.0
accuracy,0.9118685331710286,0.9118685331710286,0.9118685331710286,0.9118685331710286
macro avg,0.6732070950950906,0.8663997101062084,0.7244616781558075,8215.0
weighted avg,0.9550450109430154,0.9118685331710286,0.9274143121405569,8215.0
''',
        'docs/model_evidence/age/format_validation.csv': r'''format,size_MB,max_abs_difference,mean_abs_difference,p99_abs_difference,prediction_agreement,accuracy,recall_adult,recall_elderly,balanced_accuracy,balanced_accuracy_drop,largest_class_recall_drop,passed,reason
FP32,3.735872,1.4841556549072266e-05,1.0414356665933155e-06,8.19221168057993e-06,1.0,0.88671875,0.8984375,0.875,0.88671875,0.0,0.0,True,All checks passed
''',
        'docs/model_evidence/age/holdout_results.csv': r'''model,accuracy,balanced_accuracy,macro_f1
Majority-class baseline,0.9465611685940353,0.5,0.48627352885998376
age model,0.9118685331710286,0.8663997101062084,0.7244616781558075
''',
        'docs/model_evidence/age/model_comparison.csv': r'''format,images,accuracy,balanced_accuracy,recall_adult,recall_elderly,macro_f1,size_MB,computer_CPU_median_ms,computer_CPU_p95_ms
Keras CPU reference,512,0.87109375,0.87109375,0.921875,0.8203125,0.8707604754692735,4.077616,,
FP32,512,0.87109375,0.87109375,0.921875,0.8203125,0.8707604754692735,3.735872,13.013473500450345,16.539483850101533
''',
        'docs/model_evidence/expression/classification_report.csv': r''',precision,recall,f1-score,support
Angry,0.4580152671755725,0.48,0.46875,375.0
Disgust,0.15671641791044777,0.05343511450381679,0.07969639468690702,393.0
Fear,0.2413793103448276,0.1308411214953271,0.1696969696969697,107.0
Happy,0.8318759936406995,0.7282533054975644,0.7766233766233767,2874.0
Sad,0.5119047619047619,0.41227229146692235,0.45671800318640465,1043.0
Surprise,0.4822451317296678,0.6349924585218703,0.5481770833333334,663.0
Neutral,0.659585103724069,0.7854166666666667,0.7170221437304714,3360.0
accuracy,0.6577424844015882,0.6577424844015882,0.6577424844015882,0.6577424844015882
macro avg,0.47738885520429225,0.4607444225931667,0.45952628160820896,8815.0
weighted avg,0.6488752815514715,0.6577424844015882,0.6473361066129233,8815.0
''',
        'docs/model_evidence/expression/format_validation.csv': r'''format,max_abs_difference,mean_abs_difference,prediction_agreement,accuracy,balanced_accuracy,balanced_accuracy_drop,largest_class_recall_drop,recall_angry,recall_disgust,recall_fear,recall_happy,recall_sad,recall_surprise,recall_neutral,passed,reason
FP32,2.4437904357910156e-06,1.1718145742634078e-07,1.0,0.46875,0.46875,0.0,0.0,0.53125,0.0625,0.171875,0.734375,0.40625,0.65625,0.71875,True,All checks passed
INT8,0.33939051628112793,0.027740851044654846,0.8571428571428571,0.4419642857142857,0.4419642857142857,0.0267857142857143,0.078125,0.53125,0.03125,0.09375,0.75,0.40625,0.578125,0.703125,False,"max_abs_difference, largest_class_recall_drop, prediction_agreement"
''',
        'docs/model_evidence/expression/happy_sad_results.csv': r'''true_class,images,recall
Happy,2874,0.7282533054975644
Sad,1043,0.41227229146692235
''',
        'docs/model_evidence/expression/holdout_results.csv': r'''model,accuracy,balanced_accuracy,macro_f1
Majority-class baseline,0.3811684628474192,0.14285714285714285,0.07885010266940452
expression model,0.6577424844015882,0.4607444225931667,0.45952628160820896
''',
        'docs/model_evidence/gender/classification_report.csv': r''',precision,recall,f1-score,support
Female,0.8761261261261262,0.9048265167668152,0.8902450653189663,5159.0
Male,0.9125867901014777,0.8859315589353612,0.8990616504428659,5786.0
accuracy,0.8948378254910918,0.8948378254910918,0.8948378254910918,0.8948378254910918
macro avg,0.8943564581138019,0.8953790378510882,0.8946533578809162,10945.0
weighted avg,0.8954008087904827,0.8948378254910918,0.8949058932337112,10945.0
''',
        'docs/model_evidence/gender/female_male_class_recall.csv': r'''class,holdout_images,correct_predictions,class_recall
Female,5159,4668,0.9048265167668152
Male,5786,5126,0.8859315589353612
''',
        'docs/model_evidence/gender/format_validation.csv': r'''format,size_MB,max_abs_difference,mean_abs_difference,p99_abs_difference,prediction_agreement,accuracy,recall_female,recall_male,balanced_accuracy,balanced_accuracy_drop,largest_class_recall_drop,passed,reason
FP32,3.731884,1.7344951629638672e-05,1.2075004178768722e-06,9.697381756268442e-06,1.0,0.890625,0.92578125,0.85546875,0.890625,0.0,0.0,True,All checks passed
''',
        'docs/model_evidence/gender/holdout_results.csv': r'''model,accuracy,balanced_accuracy,macro_f1
Majority-class baseline,0.528643216080402,0.5,0.3458251150558843
gender model,0.8948378254910918,0.8953790378510882,0.8946533578809162
''',
        'docs/model_evidence/gender/model_comparison.csv': r'''format,images,accuracy,balanced_accuracy,recall_female,recall_male,macro_f1,size_MB,computer_CPU_median_ms,computer_CPU_p95_ms
Keras CPU reference,512,0.8828125,0.8828125,0.90234375,0.86328125,0.8827677794568851,4.077616,,
FP32,512,0.8828125,0.8828125,0.90234375,0.86328125,0.8827677794568851,3.731884,12.348332999863487,13.941745849979267
''',
        'gradle/wrapper/gradle-wrapper.properties': r'''distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https\://services.gradle.org/distributions/gradle-8.13-bin.zip
distributionSha256Sum=20f1b1176237254a6fc204d8434196fa11a4cfb387567519c61556e8710aed78
networkTimeout=120000
validateDistributionUrl=true
zipStoreBase=GRADLE_USER_HOME
zipStorePath=wrapper/dists
''',
        'gradle.properties': r'''org.gradle.jvmargs=-Xmx3g -Dfile.encoding=UTF-8
android.useAndroidX=true
kotlin.code.style=official

org.gradle.daemon=false
org.gradle.workers.max=2
''',
        'gradlew': r'''#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
JAR="$ROOT/gradle/wrapper/gradle-wrapper.jar"
HASH=81a82aaea5abcc8ff68b3dfcb58b3c3c429378efd98e7433460610fecd7ae45f
if [ ! -f "$JAR" ]; then
    curl --fail --location --proto '=https' --tlsv1.2 'https://raw.githubusercontent.com/gradle/gradle/v8.13.0/gradle/wrapper/gradle-wrapper.jar' -o "$JAR.download"
    ACTUAL=$(shasum -a 256 "$JAR.download" | awk '{print $1}')
    if [ "$ACTUAL" != "$HASH" ]; then rm -f "$JAR.download"; echo 'Official wrapper checksum failed.'; exit 1; fi
    mv "$JAR.download" "$JAR"
fi
JAVA=java
if [ -n "${JAVA_HOME:-}" ]; then JAVA="$JAVA_HOME/bin/java"; fi
exec "$JAVA" -Xmx64m -Dorg.gradle.appname=gradlew -classpath "$JAR" org.gradle.wrapper.GradleWrapperMain "$@"
''',
        'gradlew.bat': r'''@echo off
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%gradle\wrapper\gradle-wrapper.jar" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\prepare-wrapper.ps1"
  if errorlevel 1 exit /b 1
)
if not defined JAVA_HOME if exist "%ProgramFiles%\Android\Android Studio\jbr\bin\java.exe" set "JAVA_HOME=%ProgramFiles%\Android\Android Studio\jbr"
set "JAVA_EXE=java.exe"
if defined JAVA_HOME set "JAVA_EXE=%JAVA_HOME%\bin\java.exe"
"%JAVA_EXE%" -Xmx64m -Dorg.gradle.appname=gradlew -classpath "%ROOT%gradle\wrapper\gradle-wrapper.jar" org.gradle.wrapper.GradleWrapperMain %*
exit /b %ERRORLEVEL%
''',
        'settings.gradle.kts': r'''pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories { google(); mavenCentral() }
}
rootProject.name = "EdgeFaceAndroid"
include(":app")
''',
        'ONE_FILE_BUILD.md': r'''# EdgeFace - one script, local model files

Edit the paths at the start of Build_EdgeFace.py, then run that script on Windows.
It reads your three model ZIPs without changing them. Functions are defined first;
main() is at the end. No Base64 model data or separate repair script is needed.

The script prepares the tools, writes source, runs unit tests, builds, signs and
verifies a release APK before saving EdgeFace.apk in your project folder. It reuses
the private signing key so the app can be updated. Back up that key privately.

## Screens

- Analyze: Take photo, Choose from gallery, Browse image files, and results.
- Models: Model details, phone benchmark, Export benchmark CSV and Export benchmark JSON.
- Help: Permissions, image input, privacy, model limits, credits and installation.

The in-app collection screen has been removed. Capture screenshots yourself after
analysis. Keep permission to use the photos and protect screenshots that show faces.
Formal evaluation and its result table are prepared separately in the report.

The code creates no new collection records or permanent analysis-photo history.
Temporary camera files are removed after analysis. The original imported image is not changed.
Only measured benchmarks are saved privately. Old benchmark files are reused when their
model fingerprints match. A missing old timestamp stays unknown.

An update does not silently delete older app data or previously exported files.
If an earlier app version stored participant images, recover any needed evidence
before using Android Settings to clear its app storage. Clearing storage also removes
the saved benchmark. Phone screenshots remain outside this app's storage.

The builder archives obsolete generated source files outside the compilation folders
so they cannot accidentally remain in a rebuilt APK. It does not delete model ZIPs,
private signing files or evidence in older source/build copies.

The APK checker accepts sdkVersion and minSdkVersion. Model hashes, release signing,
minimum API 26, app ID and the absence of Internet permission are still checked.

Test the rebuilt signed APK on a phone before distributing it. A successful build
is not a camera, accuracy or gallery test. All third-party attribution is in SOURCES.md.
''',
        'app/src/main/java/com/wpretorius/edgeface/core/ImageSource.kt': r'''package com.wpretorius.edgeface.core

object ImageSource {
    const val GALLERY = "gallery"
    const val FILES = "image_files"
    fun isCamera(source: String): Boolean = source in setOf("camerax_front", "camerax_back", "system_camera")
    fun title(source: String): String = when (source) {
        GALLERY -> "Gallery image"
        FILES -> "Image file"
        "camerax_front" -> "Front camera"
        "camerax_back" -> "Back camera"
        "system_camera" -> "System camera"
        else -> "Image"
    }
}
''',
        'app/src/main/java/com/wpretorius/edgeface/core/Benchmark.kt': r'''package com.wpretorius.edgeface.core

import java.time.Instant

// A report stores measured speed, not estimated image accuracy.
data class BenchmarkReport(
    val device: String,
    val appVersion: String,
    val utc: String?,
    val results: List<BenchmarkResult>
) {
    init {
        require(device.isNotBlank() && appVersion.isNotBlank()) { "Missing benchmark device or app version." }
        require(results.size == 3 && results.map { it.attribute }.toSet() == Attribute.values().toSet()) {
            "The benchmark must contain each model exactly once."
        }
        require(utc == null || runCatching { Instant.parse(utc) }.isSuccess) { "Invalid benchmark time." }
        results.forEach { r ->
            require(r.runs == 30 && r.threads == 2) { "Unexpected benchmark run settings." }
            require(r.medianMs.isFinite() && r.p95Ms.isFinite() && r.medianMs >= 0 && r.p95Ms >= r.medianMs) {
                "Invalid benchmark measurements."
            }
            require(r.hash.matches(Regex("[0-9a-fA-F]{64}"))) { "Invalid model fingerprint." }
        }
    }

    fun matches(models: List<ModelInfo>): Boolean = models.size == 3 &&
        models.map { it.attribute }.toSet().size == 3 && results.all { r ->
            models.any { it.attribute == r.attribute && it.hash.equals(r.hash, ignoreCase = true) }
        }
}

object BenchmarkCsv {
    const val SCOPE = "On-device CPU Interpreter invocation only; fixed 224x224 RGB grey image; 5 warm-ups then 30 timed calls per model. No image loading, detection or resizing."

    // Quote text and stop spreadsheet programs from treating text as a formula.
    fun field(value: Any?): String {
        var text = value?.toString().orEmpty()
        if (text.trimStart().firstOrNull() in listOf('=', '+', '-', '@') || text.firstOrNull() in listOf('\t', '\r')) text = "'" + text
        return "\"" + text.replace("\"", "\"\"") + "\""
    }

    fun export(report: BenchmarkReport): String {
        val header = listOf("device", "app_version", "measured_at_utc", "model", "runs", "threads", "warmups", "median_ms", "p95_ms", "model_sha256", "scope")
        val rows = report.results.sortedBy { it.attribute.ordinal }.map { r ->
            listOf(report.device, report.appVersion, report.utc.orEmpty(), r.attribute.folder,
                r.runs, r.threads, 5, r.medianMs, r.p95Ms, r.hash, SCOPE).joinToString(",") { field(it) }
        }
        return header.joinToString(",") + "\r\n" + rows.joinToString("\r\n") + "\r\n"
    }
}
''',
        'app/src/main/java/com/wpretorius/edgeface/data/BenchmarkStore.kt': r'''package com.wpretorius.edgeface.data

import android.content.Context
import android.util.AtomicFile
import com.wpretorius.edgeface.core.*
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

// Only the speed report is saved here. No face photographs are saved.
class BenchmarkStore(context: Context) {
    private val file = AtomicFile(File(context.filesDir, "benchmark.json"))

    fun read(): BenchmarkReport? {
        if (!file.baseFile.exists() && !File(file.baseFile.path + ".bak").exists()) return null
        val json = file.openRead().bufferedReader(Charsets.UTF_8).use { JSONObject(it.readText()) }
        val rows = json.getJSONArray("results")
        val results = (0 until rows.length()).map { index ->
            val r = rows.getJSONObject(index)
            val attribute = Attribute.values().firstOrNull { it.folder == r.getString("model") }
                ?: throw IllegalArgumentException("Unknown benchmark model.")
            BenchmarkResult(attribute, r.getInt("runs"), r.getDouble("median_ms"), r.getDouble("p95_ms"),
                r.getInt("threads"), r.getString("model_sha256"))
        }
        // Older reports did not record a time. Leave it unknown rather than inventing one.
        val utc = if (!json.has("measured_at_utc") || json.isNull("measured_at_utc")) null else json.getString("measured_at_utc")
        return BenchmarkReport(json.getString("device"), json.getString("app_version"), utc, results)
    }

    fun asJson(report: BenchmarkReport): String {
        val rows = JSONArray()
        report.results.sortedBy { it.attribute.ordinal }.forEach { r ->
            rows.put(JSONObject().put("model", r.attribute.folder).put("runs", r.runs)
                .put("median_ms", r.medianMs).put("p95_ms", r.p95Ms).put("threads", r.threads)
                .put("model_sha256", r.hash))
        }
        return JSONObject().put("device", report.device).put("app_version", report.appVersion)
            .put("measured_at_utc", report.utc ?: JSONObject.NULL).put("warmups", 5)
            .put("results", rows).put("scope", BenchmarkCsv.SCOPE).toString(2)
    }

    fun save(report: BenchmarkReport) {
        val stream = file.startWrite()
        try {
            stream.write(asJson(report).toByteArray(Charsets.UTF_8))
            file.finishWrite(stream)
        } catch (error: Exception) {
            file.failWrite(stream)
            throw error
        }
    }
}
''',
    }


def main() -> int:
    """Run the steps in order and save the finished APK at the configured path."""
    global LOG_FILE
    args = parse_arguments()
    if sys.version_info < (3, 10):
        print("Use Python 3.10 or newer.")
        return 1
    if not args.extract_only and (os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64")):
        print("Run this builder on your 64-bit Windows laptop, not in Colab.")
        print("Use --extract-only only to inspect the source on another operating system.")
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
    root = (args.output_dir or PROJECT_DIR).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    build_records = ((root / BUILD_LOG_DIR.name) if args.output_dir else BUILD_LOG_DIR).expanduser().resolve()
    source_root = ((root / SOURCE_COPY_DIR.name) if args.output_dir else SOURCE_COPY_DIR).expanduser().resolve()
    final_apk = ((root / APK_PATH.name) if args.output_dir else APK_PATH).expanduser().resolve()
    model_paths = {"age": args.age_model, "gender": args.gender_model, "expression": args.expression_model}
    build_records.mkdir(parents=True, exist_ok=True)
    LOG_FILE = build_records / ("build_" + stamp + ".log")
    cache = args.cache_dir.expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    lock = None
    signed_env = None
    status = {"builder_version": BUILDER_VERSION, "started": stamp,
              "apk_built": False, "phone_camera_tested": False, "phone_inference_tested": False}
    result_file = build_records / ("result_" + stamp + ".json")
    try:
        lock = acquire_build_lock(cache)
        say("EDGEFACE - BUILD THE ANDROID APP")
        say("This script contains the app source and reads the three local model paths.")
        say("No training, model downloads, Python package installs, or file uploads are performed.")
        say("The first build downloads Android tools and app libraries. Keep the laptop online.")
        say("Final APK: " + str(final_apk))
        say("Build log: " + str(LOG_FILE))
        if not args.extract_only and shutil.disk_usage(cache).free < 6 * 1024 ** 3:
            raise RuntimeError("Free at least 6 GB on the build-cache drive before continuing.")
        state_file = build_records / "last_success.json"
        old_state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
        version_code = int(old_state.get("version_code", 0)) + 1
        if not 1 <= version_code <= 2100000000:
            raise ValueError("The Android version code is outside its allowed range.")
        project_id = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
        # Short local paths keep the compiler away from OneDrive and long Windows paths.
        project = cache / "work" / project_id
        say("\n[1/7] Writing the Android project and checking all three models...")
        project_files = write_project(project, version_code, model_paths)
        source_copy = source_root / stamp
        save_source_copy(project, source_copy, list(project_files))
        status.update({"source_folder": str(source_copy), "workspace": str(project),
                       "version_code": version_code, "model_files_verified": True,
                       "model_inputs": {task: str(path) for task, path in model_paths.items()}})
        save_json(result_file, status)
        if args.extract_only:
            say("\nSource and model checks passed. No build or download was attempted.")
            say("Source folder: " + str(source_copy))
            return 0
        say("\n[2/7] Finding Java 21...")
        java = prepare_java(cache, args.java_home)
        say("\n[3/7] Finding the Android SDK and installing missing packages...")
        sdk = prepare_sdk(java, cache, args.sdk_dir)
        env = make_environment(java, cache, sdk)
        say("\n[4/7] Preparing the build launcher and private release key...")
        prepare_wrapper(project, cache)
        key_folder = PRIVATE_KEY_DIR.expanduser().resolve()
        key, signed_env = prepare_signing_key(java, key_folder, env)
        # The compiler does not receive the signing password.
        say("\n[5/7] Running app unit tests and building the release APK...")
        unsigned = build_unsigned_apk(java, sdk, project, env)
        say("\n[6/7] Signing and checking the APK...")
        signed = sign_apk(java, sdk, unsigned, key, signed_env)
        signed_env.pop("EDGEFACE_SIGN_PASS", None)
        checks = verify_signed_apk(java, sdk, signed, project, env)
        if checks["version_code"] != version_code:
            raise RuntimeError("The APK version is not the version requested by this build.")
        say("\n[7/7] Saving the finished APK...")
        if final_apk.is_file():
            previous = build_records / "previous_apks"
            previous.mkdir(exist_ok=True)
            shutil.copy2(final_apk, previous / ("EdgeFace_" + stamp + ".apk"))
        atomic_write(final_apk, signed.read_bytes())
        if file_hash(final_apk) != checks["apk_sha256"]:
            raise RuntimeError("The final APK copy failed its fingerprint check.")
        atomic_write(final_apk.with_name(final_apk.name + ".sha256"),
                     (checks["apk_sha256"] + "  " + final_apk.name + "\n").encode("utf-8"))
        # Complete the readable source copy, but keep keys and passwords outside it.
        for name in ("gradle/wrapper/gradle-wrapper.jar", "local.properties"):
            target = source_copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(project / name, target)
        reports = project / "app/build/reports/tests/testReleaseUnitTest"
        if reports.is_dir():
            shutil.copytree(reports, build_records / ("unit_tests_" + stamp))
        status.update(checks)
        status.update({"apk_built": True, "app_unit_test_task_succeeded": True,
                       "apk": str(final_apk), "finished": datetime.now(timezone.utc).isoformat(),
                       "java_home": str(java), "android_sdk": str(sdk)})
        save_json(result_file, status)
        save_json(state_file, {"version_code": version_code, "apk_sha256": checks["apk_sha256"],
                               "signing_folder": str(key_folder), "finished": status["finished"]})
        say("\nBUILD COMPLETE")
        say("APK: " + str(final_apk))
        say(f"Size: {final_apk.stat().st_size / 1024 ** 2:.1f} MB")
        say("All three original model fingerprints and the APK signature were verified.")
        say("Install this APK on your Android phone and test Gallery, Files, the camera and offline predictions.")
        say("After phone testing, this same APK is the file to send to the professor.")
        say("The professor does not need Python or Android Studio.")
        say("Keep your signing folder private and back it up: " + str(key_folder))
        say("Readable source: " + str(source_copy))
        show_output_folder(final_apk.parent)
        return 0
    except KeyboardInterrupt:
        status["error"] = "Build cancelled by the user."
        save_json(result_file, status)
        say("\nBuild stopped. Existing trained models were not changed. Run this script again to retry.")
        return 130
    except Exception as error:
        status["error"] = str(error)
        save_json(result_file, status)
        say("\nBUILD STOPPED: " + str(error))
        say("No new APK is approved by this run. An existing EdgeFace.apk may belong to an older run.")
        say("Keep this script and the log. Run the same script again after fixing the reported problem.")
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            traceback.print_exc(file=stream)
        return 1
    finally:
        if signed_env is not None:
            signed_env.pop("EDGEFACE_SIGN_PASS", None)
        if lock is not None:
            lock.close()


# The program starts here.
if __name__ == "__main__":
    exit_code = main()
    if os.name == "nt" and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(exit_code)
