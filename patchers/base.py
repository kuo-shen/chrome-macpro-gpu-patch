#!/usr/bin/env python3
"""
Base Browser Patcher class for Chromium-based browsers on macOS.
Handles common workflows: backup, pattern scanning & patching, launcher compilation,
codesigning with LocalCodeSigner, and signature validation.
"""
import os
import sys
import shutil
import subprocess
import glob
import time
import shlex

def notify(title, message):
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{message}" with title "{title}" sound name "Glass"'
        ], capture_output=True)
    except Exception:
        pass

def run(cmd):
    if isinstance(cmd, (list, tuple)):
        cmd_list = [str(x) for x in cmd]
        display_str = " ".join(shlex.quote(str(x)) for x in cmd)
    else:
        cmd_list = shlex.split(str(cmd))
        display_str = str(cmd)
    print(f"[RUN] {display_str}")
    res = subprocess.run(cmd_list, shell=False, capture_output=True, text=True)
    if res.returncode != 0 and res.stderr:
        print(f"[ERR] {res.stderr.strip()}")
    return res

def safe_copy(src, dst):
    subprocess.run(["cp", "-f", src, dst], check=True)

# --- Chromium milestone awareness -------------------------------------------
# M141: the chrome://flags#use-angle entry was removed on macOS
#       (https://chromium-review.googlesource.com/c/chromium/src/+/6965659)
# M152: the ANGLE CGL/GL backend was removed from GetAllowedGLImplementations()
#       in ui/gl/init/gl_factory_mac.cc
#       (https://chromium-review.googlesource.com/c/chromium/src/+/7898546,
#        https://issues.chromium.org/issues/519633318).
#       From M152 on, --use-angle=gl is rejected rather than ignored:
#       gl_factory.cc returns kGLImplementationNone and GPU init fails.
ANGLE_FLAG_ENTRY_REMOVED_MILESTONE = 141
ANGLE_GL_BACKEND_REMOVED_MILESTONE = 152
VALIDATED_MILESTONES = (152, 153, 158)
# Pattern presence was checked against M157 during Phase 0-1 diagnostics;
# M158 (Brave v158.1.100.58) is fully runtime-validated on hardware.
PATTERN_VERIFIED_MILESTONES = (157,)
KNOWN_MILESTONES = VALIDATED_MILESTONES + PATTERN_VERIFIED_MILESTONES

GPU_PATCH_MARKER = "--chromium-gpu-patch-profile="
GPU_PROFILES = ("compat", "accelerated", "custom")
DEFAULT_GPU_PROFILE = "compat"


def parse_milestone(version):
    """Extract the Chromium milestone from a framework version directory name.

    e.g. "153.0.8010.53" -> 153. Returns None if it cannot be determined.
    """
    if not version:
        return None
    head = str(version).split(".")[0]
    return int(head) if head.isdigit() else None


class BaseBrowserPatcher:
    name = "Base Chromium Browser"
    slug = "base"
    possible_app_paths = []
    bundle_id = ""
    app_bundle_name = ""
    framework_name = ""
    framework_binary_name = ""
    launcher_name = ""
    launcher_src = ""
    backup_base_dir = ""
    certificate_name = "LocalCodeSigner"
    default_gpu_profile = DEFAULT_GPU_PROFILE
    default_skip_patches = ()
    use_dynamic_angle = False

    def __init__(self, repo_root=None, app_path=None):
        if repo_root is None:
            # Fallback to repo root directory (parent of patchers/)
            self.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        else:
            self.repo_root = repo_root
        self.custom_app_path = app_path
        self.app_path = self.find_app()

    def find_via_mdfind(self):
        """Locates the .app bundle dynamically anywhere on macOS using Spotlight."""
        # 1. Search by Bundle Identifier first
        if self.bundle_id:
            try:
                cmd = ["mdfind", f"kMDItemCFBundleIdentifier == '{self.bundle_id}'"]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                if res.returncode == 0:
                    for line in res.stdout.strip().splitlines():
                        line = line.strip()
                        if line.endswith(".app") and os.path.exists(line):
                            return line
            except Exception:
                pass

        # 2. Search by App Bundle filename
        if self.app_bundle_name:
            try:
                cmd = ["mdfind", f"kMDItemFSName == '{self.app_bundle_name}'"]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                if res.returncode == 0:
                    for line in res.stdout.strip().splitlines():
                        line = line.strip()
                        if line.endswith(".app") and os.path.exists(line):
                            return line
            except Exception:
                pass

        return None

    def find_app(self):
        """Resolves the browser installation directory using multiple discovery tiers."""
        # Tier 1: Explicit custom path passed by user
        if self.custom_app_path:
            expanded = os.path.abspath(os.path.expanduser(self.custom_app_path))
            return expanded

        # Tier 2: Environment variable override (<BROWSER>_APP_PATH)
        env_key = f"{self.slug.upper()}_APP_PATH"
        if env_key in os.environ:
            env_path = os.path.abspath(os.path.expanduser(os.environ[env_key]))
            if os.path.exists(env_path):
                return env_path

        # Tier 3: Check standard predefined installation paths
        for path in self.possible_app_paths:
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                return expanded

        # Tier 4: Dynamic system-wide discovery via macOS Spotlight (mdfind)
        found = self.find_via_mdfind()
        if found:
            return found

        # Tier 5: Fallback to default expected path
        if self.possible_app_paths:
            return os.path.expanduser(self.possible_app_paths[0])
        return ""

    def is_installed(self):
        return bool(self.app_path and os.path.exists(self.app_path))

    @property
    def frameworks_dir(self):
        return os.path.join(self.app_path, "Contents/Frameworks")

    @property
    def framework_dir(self):
        return os.path.join(self.frameworks_dir, self.framework_name)

    @property
    def versions_dir(self):
        return os.path.join(self.framework_dir, "Versions")

    @property
    def current_link(self):
        return os.path.join(self.versions_dir, "Current")

    def get_current_version(self):
        if os.path.islink(self.current_link) and os.path.exists(self.current_link):
            return os.path.basename(os.path.realpath(self.current_link))
        if os.path.isdir(self.versions_dir):
            candidates = [
                d for d in os.listdir(self.versions_dir)
                if os.path.isdir(os.path.join(self.versions_dir, d)) and not d.startswith(".") and d != "Current"
            ]
            if candidates:
                candidates.sort(key=lambda s: [int(x) if x.isdigit() else 0 for x in s.split(".")])
                return candidates[-1]
        return None

    def get_ver_dir(self, version):
        return os.path.join(self.versions_dir, version)

    def get_framework_bin(self, version):
        return os.path.join(self.get_ver_dir(version), self.framework_binary_name)

    def get_launcher_dst(self):
        return os.path.join(self.app_path, "Contents/MacOS", self.launcher_name)

    def is_launcher_patched(self, launcher_path=None):
        """
        Verify that the launcher is our custom compiled binary.

        V2 launchers carry a --chromium-gpu-patch-profile=<name> marker and may
        use any supported profile, so the old requirement that the launcher
        embed specific --disable-* flags no longer applies. V1 launchers are
        still recognized for backward compatibility.
        """
        if not launcher_path:
            launcher_path = self.get_launcher_dst()
        if not os.path.exists(launcher_path):
            return False

        try:
            with open(launcher_path, "rb") as f:
                content = f.read()
        except Exception:
            return False

        if GPU_PATCH_MARKER.encode("utf-8") in content:
            # V2 launcher: marker is authoritative. Require an angle switch
            # so a partially compiled/foreign binary is not accepted.
            if b"--use-angle=" not in content:
                return False
        else:
            # Legacy V1 launcher.
            if (b"--use-angle=gl" not in content
                    or b"--disable-video-capture-use-gpu-memory-buffer" not in content
                    or b"ScreenCaptureKitMacScreen" not in content):
                return False

        res = subprocess.run(["codesign", "-dvvv", launcher_path], capture_output=True, text=True)
        if f"Authority={self.certificate_name}" not in res.stderr and f"Authority={self.certificate_name}" not in res.stdout:
            return False

        return True

    def get_backup_dir(self, version):
        return os.path.join(os.path.expanduser(self.backup_base_dir), version)

    def is_already_patched(self, version):
        """Override in subclasses to provide specific fast verification."""
        raise NotImplementedError

    def backup_original(self, version):
        backup_dir = self.get_backup_dir(version)
        os.makedirs(backup_dir, exist_ok=True)
        fw_bin = self.get_framework_bin(version)
        fw_bak = os.path.join(backup_dir, f"{self.framework_binary_name}.original")
        launcher_dst = self.get_launcher_dst()
        launcher_bak = os.path.join(backup_dir, f"{self.launcher_name}.original")

        if os.path.exists(fw_bin) and not os.path.exists(fw_bak):
            print(f"[*] Backing up original framework to {fw_bak}...")
            safe_copy(fw_bin, fw_bak)

        if os.path.exists(launcher_dst) and not os.path.exists(launcher_bak):
            print(f"[*] Backing up original launcher to {launcher_bak}...")
            safe_copy(launcher_dst, launcher_bak)

    def clean_obsolete_versions(self, version):
        """Moves obsolete version directories out of the framework bundle to prevent codesign verification failures."""
        backup_dir = self.get_backup_dir(version)
        os.makedirs(backup_dir, exist_ok=True)
        if os.path.isdir(self.versions_dir):
            for d in os.listdir(self.versions_dir):
                d_path = os.path.join(self.versions_dir, d)
                if os.path.isdir(d_path) and not d.startswith(".") and d != "Current" and d != version:
                    print(f"[*] Moving obsolete version {d} out of bundle to {backup_dir}...")
                    dst_obsolete = os.path.join(backup_dir, d)
                    if os.path.exists(dst_obsolete):
                        run(f'mv "{d_path}" "{dst_obsolete}_{int(time.time())}"')
                    else:
                        run(f'mv "{d_path}" "{backup_dir}/"')

    def restore(self, version):
        backup_dir = self.get_backup_dir(version)
        fw_bak = os.path.join(backup_dir, f"{self.framework_binary_name}.original")
        launcher_bak = os.path.join(backup_dir, f"{self.launcher_name}.original")
        fw_bin = self.get_framework_bin(version)
        launcher_dst = self.get_launcher_dst()

        restored = False
        if os.path.exists(fw_bak):
            print(f"[*] Restoring {self.name} Framework from backup...")
            safe_copy(fw_bak, fw_bin)
            restored = True
        if os.path.exists(launcher_bak):
            print(f"[*] Restoring {self.name} launcher from backup...")
            safe_copy(launcher_bak, launcher_dst)
            restored = True

        if restored:
            self.clean_obsolete_versions(version)
            print(f"[*] Stripping quarantine and extended attributes from {self.name}...")
            run(f'xattr -cr "{self.app_path}"')
            print(f"[*] Re-signing restored {self.name} with {self.certificate_name}...")
            run(f'codesign --force --sign "{self.certificate_name}" "{fw_bin}"')
            run(f'codesign --force --sign "{self.certificate_name}" "{launcher_dst}"')
            res = run(f'codesign --force --deep --sign "{self.certificate_name}" "{self.app_path}"')
            if res.returncode != 0:
                print(f"[*] Retrying with ad-hoc signature as fallback...")
                run(f'codesign --force --deep -s - "{self.app_path}"')
            
            # Verify signature
            v = run(f'codesign -v "{self.app_path}"')
            if v.returncode == 0:
                print(f"[+] Restored {self.name} {version} successfully and verified valid signature!")
            else:
                print(f"[!] Warning: Restored {self.name} signature verification reported: {v.stderr.strip()}")
            return True
        else:
            print(f"[-] No backup found for {self.name} {version}.")
            return False

    def get_patches(self, version):
        """Return list of (name, orig_bytes, patched_bytes) or regex scanner callable."""
        raise NotImplementedError

    def ensure_certificate(self):
        """Ensures the code-signing certificate exists in the user keychain, creating it if needed."""
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user and sudo_user != "root":
            check_cmd = ["sudo", "-u", sudo_user, "security", "find-certificate", "-c", self.certificate_name]
        else:
            check_cmd = ["security", "find-certificate", "-c", self.certificate_name]

        res = subprocess.run(check_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            cert_script = os.path.join(self.repo_root, "setup_certificate.sh")
            if os.path.exists(cert_script):
                print(f"[*] Certificate '{self.certificate_name}' not found. Automatically setting up certificate...")
                subprocess.run(["bash", cert_script], check=True)

    def pre_patch_hook(self, version):
        """Standard hook: inject clean ANGLE dylibs (libEGL.dylib, libGLESv2.dylib)."""
        ver_dir = self.get_ver_dir(version)
        lib_dir = os.path.join(ver_dir, "Libraries")
        os.makedirs(lib_dir, exist_ok=True)
        ref_angle_lib = os.path.join(self.repo_root, "angle_dylibs")

        for dylib in ["libEGL.dylib", "libGLESv2.dylib"]:
            src = os.path.join(ref_angle_lib, dylib)
            dst = os.path.join(lib_dir, dylib)
            if not os.path.exists(dst):
                if os.path.exists(src):
                    print(f"[*] {self.name} {version} is missing {dylib}. Injecting bundled ANGLE dylib...")
                    safe_copy(src, dst)
                else:
                    print(f"[!] Warning: Reference dylib not found at {src}")
            else:
                print(f"[*] {self.name} {version} has native {dylib}, keeping original.")

    def post_patch_hook(self, version):
        """Standard hook: clean obsolete versions, ensure Versions/Current and framework root symlinks exist."""
        # 1. Ensure obsolete versions don't break bundle signature
        self.clean_obsolete_versions(version)

        # 2. Ensure Versions/Current symlink points to version
        current_link = os.path.join(self.versions_dir, "Current")
        if not os.path.islink(current_link) or not os.path.exists(current_link):
            print(f"[*] Ensuring Versions/Current symlink points to {version}...")
            run(f'ln -sfn "{version}" "{current_link}"')

        # 3. Ensure Libraries symlink exists at framework root
        fw_lib_link = os.path.join(self.framework_dir, "Libraries")
        if not os.path.islink(fw_lib_link) or not os.path.exists(fw_lib_link):
            print(f"[*] Ensuring Libraries symlink exists at {self.name} Framework root...")
            run(f'ln -sfn "Versions/Current/Libraries" "{fw_lib_link}"')

    def report_milestone_context(self, version):
        """Print what the detected Chromium milestone implies for this patch.

        Informational only - this never blocks patching. Even on builds where
        the GL backend is still present, the launcher shim's zero-copy and
        Skia Graphite mitigations remain useful.
        """
        milestone = parse_milestone(version)
        lo, hi = min(VALIDATED_MILESTONES), max(VALIDATED_MILESTONES)
        known_hi = max(KNOWN_MILESTONES)

        if milestone is None:
            print(f"[*] Could not determine Chromium milestone from version '{version}'.")
            print(f"    Binary patterns are validated against M{lo}-M{hi}; proceeding anyway.")
            return None

        print(f"[*] Detected Chromium milestone: M{milestone}")

        if milestone < ANGLE_GL_BACKEND_REMOVED_MILESTONE:
            print(f"    Note: the ANGLE GL backend is still present in "
                  f"GetAllowedGLImplementations() on M{milestone}.")
            print("    '--use-angle=gl' works on this build WITHOUT binary patching.")
            if milestone >= ANGLE_FLAG_ENTRY_REMOVED_MILESTONE:
                print(f"    (chrome://flags#use-angle was removed on macOS in "
                      f"M{ANGLE_FLAG_ENTRY_REMOVED_MILESTONE}, so pass the flag on the")
                print("     command line rather than setting it in the browser UI.)")
            print("    Patching is still worthwhile for the zero-copy / Skia Graphite fixes.")
        elif milestone in PATTERN_VERIFIED_MILESTONES:
            print(f"    Pattern presence was verified read-only on M{milestone}.")
            print("    Runtime validation of the patched browser is still pending.")
        elif milestone > known_hi:
            print(f"    Warning: binary patterns are validated against M{lo}-M{hi} "
                  f"(pattern-checked through M{known_hi}).")
            print(f"    M{milestone} is newer - if a pattern below is not found, it has "
                  f"most likely drifted and needs re-deriving.")

        return milestone

    def apply_binary_patches(self, version, dry_run=False, skip_patches=None):
        fw_bin = self.get_framework_bin(version)
        backup_dir = self.get_backup_dir(version)
        fw_bak = os.path.join(backup_dir, f"{self.framework_binary_name}.original")
        skip_set = set(skip_patches or ())

        if not dry_run and os.path.exists(fw_bak):
            print(f"[*] Resetting to clean original {self.name} framework from backup before patching...")
            safe_copy(fw_bak, fw_bin)

        if not os.path.exists(fw_bin):
            print(f"[!] Error: Framework binary not found at {fw_bin}")
            return False

        milestone = self.report_milestone_context(version)

        action = "Simulating" if dry_run else "Reading"
        print(f"[*] {action} {self.name} framework binary ({os.path.getsize(fw_bin):,} bytes)...")
        with open(fw_bin, "rb") as f:
            data = bytearray(f.read())

        patches = self.get_patches(version)
        applied_count = 0
        skipped_count = 0
        failed_required = []
        failed_optional = []

        for patch_idx, item in enumerate(patches, start=1):
            optional = False
            custom_func = None
            orig = pat = None

            if len(item) == 2 and callable(item[1]):
                p_name, custom_func = item
            elif len(item) == 3 and callable(item[1]):
                p_name, custom_func, optional = item
            elif len(item) == 3:
                p_name, orig, pat = item
            elif len(item) == 4:
                p_name, orig, pat, optional = item
            else:
                print(f"[!] Warning: malformed patch entry: {item!r}")
                failed_required.append(str(item))
                continue

            if patch_idx in skip_set:
                print(f"[*] Skipping {p_name} (--skip-patches).")
                skipped_count += 1
                continue

            if custom_func is not None:
                success = custom_func(data)
                if success:
                    applied_count += 1
                elif optional:
                    failed_optional.append(p_name)
                    print(f"[!] Optional {p_name} not applied; continuing.")
                else:
                    failed_required.append(p_name)
                    print(f"[!] Required {p_name} not applied.")
                continue

            idx = data.find(orig)
            if idx != -1:
                data[idx:idx+len(pat)] = pat
                print(f"[+] {p_name} applied successfully at {hex(idx)}!")
                applied_count += 1
            elif data.find(pat) != -1:
                print(f"[*] {p_name} is already applied.")
                applied_count += 1
            else:
                if optional:
                    failed_optional.append(p_name)
                    print(f"[!] Optional {p_name} pattern not found; continuing.")
                else:
                    failed_required.append(p_name)
                    print(f"[!] Warning: {p_name} pattern not found!")
                if milestone and milestone > max(KNOWN_MILESTONES):
                    print(f"    (M{milestone} is newer than the pattern-checked "
                          f"M{min(KNOWN_MILESTONES)}-M{max(KNOWN_MILESTONES)} range.)")

        if failed_required:
            print(f"[!] Patching incomplete: {len(failed_required)} required patch(es) failed.")
            for p_name in failed_required:
                print(f"    - {p_name}")
            return False

        if failed_optional:
            print(f"[*] {len(failed_optional)} optional patch(es) were not applied:")
            for p_name in failed_optional:
                print(f"    - {p_name}")

        active_total = len(patches) - skipped_count
        skip_note = f", {skipped_count} skipped" if skipped_count else ""

        if dry_run:
            print(f"[+] Dry run complete: all required patches match "
                  f"({applied_count}/{active_total} confirmed/applied{skip_note}). No binary was modified.")
            return True

        print(f"[+] All required patches confirmed/applied "
              f"({applied_count}/{active_total} confirmed/applied{skip_note}). Writing updated binary...")
        with open(fw_bin, "wb") as f:
            f.write(data)
        return True

    def install_launcher(self, version):
        launcher_src_path = os.path.join(self.repo_root, self.launcher_src)
        launcher_dst = self.get_launcher_dst()

        if not os.path.exists(launcher_src_path):
            print(f"[!] Error: Launcher source not found at {launcher_src_path}")
            return False

        profile = getattr(self, "gpu_profile", None) or self.default_gpu_profile
        if profile not in GPU_PROFILES:
            print(f"[!] Unknown GPU profile '{profile}', using {self.default_gpu_profile}.")
            profile = self.default_gpu_profile

        dyn_note = ", dynamic ANGLE: on" if self.use_dynamic_angle else ""
        print(f"[*] Compiling native C launcher for {self.name} "
              f"(default profile: {profile}{dyn_note})...")
        tmp_launcher = launcher_dst + ".new"
        profile_define = f'-DCHROME_GPU_DEFAULT_PROFILE="{profile}"'
        clang_cmd = ["clang", "-O2", profile_define]
        if self.use_dynamic_angle:
            clang_cmd.append("-DCHROME_GPU_USE_DYNAMIC_ANGLE")
        clang_cmd.extend([launcher_src_path, "-o", tmp_launcher])
        res = run(clang_cmd)
        if res.returncode == 0:
            os.replace(tmp_launcher, launcher_dst)
            print(f"[+] {self.name} launcher installed successfully!")
            return True
        else:
            print(f"[!] Failed to compile launcher for {self.name}!")
            return False

    def codesign(self, version):
        ver_dir = self.get_ver_dir(version)
        fw_bin = self.get_framework_bin(version)
        launcher_dst = self.get_launcher_dst()

        print(f"[*] Stripping quarantine and extended attributes from {self.name}...")
        run(f'xattr -cr "{self.app_path}"')

        print(f"[*] Re-signing {self.name} components with {self.certificate_name}...")

        # 1. Sign all dylibs in Libraries/
        lib_dir = os.path.join(ver_dir, "Libraries")
        if os.path.isdir(lib_dir):
            for f in os.listdir(lib_dir):
                if f.endswith(".dylib"):
                    run(f'codesign --force --sign "{self.certificate_name}" "{os.path.join(lib_dir, f)}"')

        # 2. Sign Helpers
        helpers_dir = os.path.join(ver_dir, "Helpers")
        if os.path.isdir(helpers_dir):
            for h in os.listdir(helpers_dir):
                h_path = os.path.join(helpers_dir, h)
                if h.endswith(".app"):
                    run(f'codesign --force --deep --sign "{self.certificate_name}" "{h_path}"')
                elif os.path.isfile(h_path) and os.access(h_path, os.X_OK):
                    run(f'codesign --force --sign "{self.certificate_name}" "{h_path}"')

        # 3. Sign framework binary, version dir, framework bundle, launcher, and main app
        run(f'codesign --force --sign "{self.certificate_name}" "{fw_bin}"')
        run(f'codesign --force --sign "{self.certificate_name}" "{ver_dir}"')
        run(f'codesign --force --sign "{self.certificate_name}" "{self.framework_dir}"')
        run(f'codesign --force --sign "{self.certificate_name}" "{launcher_dst}"')
        run(f'codesign --force --deep --sign "{self.certificate_name}" "{self.app_path}"')

    def verify_signature(self):
        print(f"[*] Verifying {self.name} code signature...")
        v = run(f'codesign -v "{self.app_path}"')
        if v.returncode == 0:
            print(f"[+] Signature VALID! {self.name} is ready to run.")
            return True
        else:
            print(f"[!] Codesign verification warning: {v.stderr.strip()}")
            return False

    def grant_privacy_permissions(self, auto_yes=False):
        """
        Opt-in tool to grant Camera and Microphone authorization in macOS TCC database.
        
        Because the application is re-signed with a local certificate (LocalCodeSigner),
        macOS invalidates previous vendor permissions and silently suppresses interactive
        camera/microphone prompts ('Policy disallows prompt').
        This method dynamically extracts the application's designated requirement, compiles
        the binary csreq structure, creates a safety backup of TCC.db, and inserts/updates
        authorization records in:
        ~/Library/Application Support/com.apple.TCC/TCC.db
        """
        bundle_id = self.bundle_id
        if not bundle_id and os.path.exists(self.app_path):
            info_plist = os.path.join(self.app_path, "Contents/Info.plist")
            if os.path.exists(info_plist):
                try:
                    import plistlib
                    with open(info_plist, "rb") as f:
                        plist = plistlib.load(f)
                    bundle_id = plist.get("CFBundleIdentifier", "")
                except Exception:
                    pass

        if not bundle_id:
            print(f"[*] Skipping privacy permissions: bundle_id not set for {self.name}.")
            return False

        if not os.path.exists(self.app_path):
            print(f"[!] Cannot grant permissions: {self.app_path} does not exist.")
            return False

        tcc_db_dir = os.path.expanduser("~/Library/Application Support/com.apple.TCC")
        tcc_db = os.path.join(tcc_db_dir, "TCC.db")

        if not os.path.exists(tcc_db):
            print(f"[!] User TCC database not found at {tcc_db}.")
            return False

        print(f"\n[*] Privacy Permission Notice:")
        print(f"    Modifying macOS TCC permissions directly requires user consent and")
        print(f"    may fail unless Terminal has Full Disk Access (FDA) in System Settings.")
        print(f"    This will inject Camera & Microphone authorization for {self.name}")
        print(f"    ({bundle_id}) into your user TCC database.")

        if not auto_yes:
            if not sys.stdin.isatty():
                print("[-] Non-interactive session detected and -y/--yes was not specified. Aborting.")
                return False
            try:
                choice = input(f"\nProceed with granting Camera & Microphone permissions for {self.name}? [y/N]: ").strip().lower()
                if choice not in ("y", "yes"):
                    print("[-] Canceled by user.")
                    return False
            except (KeyboardInterrupt, EOFError):
                print("\n[-] Canceled by user.")
                return False

        print(f"[*] Configuring macOS privacy permissions (Camera & Microphone) for {self.name}...")

        # 1. Extract designated requirement from codesign
        res = subprocess.run(["codesign", "-d", "-r-", self.app_path], capture_output=True, text=True)
        req_str = ""
        for line in (res.stdout + "\n" + res.stderr).splitlines():
            if "designated =>" in line:
                req_str = line.split("designated =>", 1)[1].strip()
                break

        if not req_str:
            print(f"[!] Warning: Could not extract designated code requirement from {self.app_path}.")
            return False

        # 2. Compile requirement into binary csreq format using csreq tool
        import tempfile
        csreq_blob = None
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                tmp_path = tmp.name
            p = subprocess.run(
                ["csreq", "-r-", "-b", tmp_path],
                input=req_str.encode("utf-8"),
                capture_output=True
            )
            if p.returncode == 0 and os.path.exists(tmp_path):
                with open(tmp_path, "rb") as f:
                    csreq_blob = f.read()
            else:
                err_msg = p.stderr.decode("utf-8", errors="ignore").strip() if p.stderr else "csreq failed"
                print(f"[!] Warning: csreq compilation failed: {err_msg}")
        except Exception as e:
            print(f"[!] Warning: Failed to run csreq compiler: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        if not csreq_blob:
            print(f"[!] Warning: Could not compile binary code requirement for {bundle_id}.")
            return False

        # 3. Create safety backup of TCC.db before modification
        import time
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        bak_path = f"{tcc_db}.bak_{timestamp}"
        try:
            shutil.copy2(tcc_db, bak_path)
            print(f"[+] Safety backup of TCC.db created at:")
            print(f"    {bak_path}")
        except Exception as e:
            print(f"[!] Error: Failed to create safety backup of TCC.db: {e}")
            print("[-] Aborting modification to protect database integrity.")
            return False

        # 4. Update the user TCC database
        import sqlite3
        services = [
            ("kTCCServiceCamera", "Camera"),
            ("kTCCServiceMicrophone", "Microphone")
        ]

        try:
            conn = sqlite3.connect(tcc_db)
            cur = conn.cursor()
            for service_id, label in services:
                # Remove any existing path-based or bundle-id entries to prevent duplicate toggles in System Settings
                cur.execute(
                    "DELETE FROM access WHERE service = ? AND (client = ? OR client = ? OR client LIKE ?)",
                    (service_id, bundle_id, self.app_path, f"%{self.name}%")
                )
                cur.execute("""
                    INSERT OR REPLACE INTO access (
                        service, client, client_type, auth_value, auth_reason, auth_version, csreq, indirect_object_identifier
                    ) VALUES (?, ?, 0, 2, 4, 1, ?, 'UNUSED')
                """, (service_id, bundle_id, sqlite3.Binary(csreq_blob)))
            conn.commit()
            conn.close()
            print(f"[+] Successfully granted Camera & Microphone permissions for {self.name} ({bundle_id}).")
        except Exception as e:
            print(f"[!] Warning: Failed to update TCC database: {e}")
            print("    (If macOS blocked access, grant Terminal Full Disk Access in System Settings > Privacy & Security).")
            print(f"    You can restore your TCC database anytime from: {bak_path}")
            return False

        # 5. Refresh macOS privacy daemon (tccd)
        try:
            uid = os.getuid()
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/com.apple.tccd"], capture_output=True)
            print(f"[+] macOS privacy daemon (tccd) refreshed.")
        except Exception as e:
            print(f"[*] Note: Could not kickstart tccd: {e}")

        # 6. Check if application is currently running
        try:
            if self.launcher_name:
                res = subprocess.run(["pgrep", "-x", self.launcher_name], capture_output=True, text=True)
                if res.returncode == 0:
                    print(f"[*] Note: {self.name} is currently running. Please restart it (Cmd+Q) for permissions to take effect.")
        except Exception:
            pass

        return True

    def probe_features(self):
        """Run a headless feature probe and return (renderer, features, error).

        The probe covers WebGL/WebGL2, WebGPU, 2D canvas availability, media
        capability queries for common codecs, and capture API availability.
        It uses an isolated temporary profile so it does not touch the user's
        browser session, and streams Base64-encoded JSON over console logging
        so headless macOS builds where --dump-dom stalls on virtual VSync
        still return in ~1-2 seconds.
        """
        launcher_dst = self.get_launcher_dst()
        if not os.path.exists(launcher_dst):
            return "", {}, f"Executable not found at {launcher_dst}"

        import base64
        import json
        import re
        import select
        import shutil
        import subprocess
        import tempfile

        probe_html = """<!DOCTYPE html><html><body>
<script>
(function () {
  const out = {};
  function emit(tag) {
    try {
      const payload = btoa(unescape(encodeURIComponent(JSON.stringify(out))));
      console.log(tag + ':' + payload);
    } catch (e) {
      console.log(tag + '_ERR:' + String(e));
    }
  }

  try {
    const c = document.createElement('canvas');
    const gl2 = c.getContext('webgl2');
    const gl = gl2 || c.getContext('webgl') || c.getContext('experimental-webgl');
    out.webgl = !!gl;
    out.webgl2 = !!gl2;
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      out.renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'ENABLED_NO_INFO';
      out.vendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : '';
    } else {
      out.renderer = 'DISABLED';
    }
  } catch (e) {
    out.renderer = 'ERROR:' + e.message;
  }

  try {
    out.canvas2d = !!document.createElement('canvas').getContext('2d');
  } catch (e) {
    out.canvas2d = false;
  }

  try {
    out.userMedia = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    out.displayMedia = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  } catch (e) {}

  emit('GPU_PROBE_PARTIAL');

  (async function () {
    const withTimeout = function (promise, ms, fallback) {
      return Promise.race([
        promise,
        new Promise(function (resolve) { setTimeout(function () { resolve(fallback); }, ms); })
      ]);
    };

    const webgpuTask = (async function () {
      try {
        out.webgpu = !!navigator.gpu;
        if (navigator.gpu) {
          const adapter = await withTimeout(navigator.gpu.requestAdapter(), 1500, null);
          out.webgpuAdapter = !!adapter;
          if (adapter && adapter.info) {
            out.webgpuInfo = {
              vendor: adapter.info.vendor,
              architecture: adapter.info.architecture,
              device: adapter.info.device,
              description: adapter.info.description
            };
          }
        }
      } catch (e) {
        out.webgpu = false;
        out.webgpuError = String(e);
      }
    })();

    const mediaTask = (async function () {
      out.media = {};
      const codecs = {
        h264: 'video/mp4; codecs="avc1.42E01E"',
        vp9: 'video/mp4; codecs="vp09.00.10.08"',
        av1: 'video/mp4; codecs="av01.0.04M.08"',
        hevc: 'video/mp4; codecs="hvc1.1.6.L93.B0"'
      };
      if (navigator.mediaCapabilities && navigator.mediaCapabilities.decodingInfo) {
        await Promise.all(Object.keys(codecs).map(async function (name) {
          try {
            out.media[name] = await withTimeout(
              navigator.mediaCapabilities.decodingInfo({
                type: 'file',
                video: {
                  contentType: codecs[name],
                  width: 1920,
                  height: 1080,
                  bitrate: 4000000,
                  framerate: 30
                }
              }),
              1500,
              { error: 'timeout' }
            );
          } catch (e) {
            out.media[name] = { error: String(e) };
          }
        }));
      } else {
        out.mediaError = 'mediaCapabilities unavailable';
      }
    })();

    await Promise.all([webgpuTask, mediaTask]);
    emit('GPU_PROBE_DONE');
  })();
})();
</script>
</body></html>"""

        # Chrome, Brave, and Helium support --headless=new; Opera works best with --headless
        flag_headless = "--headless" if self.slug == "opera" else "--headless=new"
        tmpdir = tempfile.mkdtemp(prefix="chrome-gpu-probe-")
        probe_file = os.path.join(tmpdir, "probe.html")
        user_data_dir = os.path.join(tmpdir, "profile")
        with open(probe_file, "w", encoding="utf-8") as f:
            f.write(probe_html)

        cmd = [
            launcher_dst,
            flag_headless,
            "--use-mock-keychain",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-extensions",
            "--disable-sync",
            "--disable-default-apps",
            "--mute-audio",
            "--enable-logging=stderr",
            "--log-level=0",
            f"--user-data-dir={user_data_dir}",
            f"file://{probe_file}",
        ]

        def _decode_tag(line, tag):
            m = re.search(rf"{tag}:([A-Za-z0-9+/=]+)", line)
            if not m:
                return None
            try:
                return json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
            except Exception:
                return None

        proc = None
        features = {}
        done = False
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            deadline = time.monotonic() + 15.0
            fd = proc.stderr.fileno()
            buf = ""
            while time.monotonic() < deadline:
                remaining = max(0.05, deadline - time.monotonic())
                ready, _, _ = select.select([fd], [], [], min(0.5, remaining))
                if ready:
                    chunk = os.read(fd, 8192).decode("utf-8", errors="ignore")
                    if not chunk:
                        break
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        parsed_done = _decode_tag(line, "GPU_PROBE_DONE")
                        if parsed_done is not None:
                            features = parsed_done
                            done = True
                            break
                        parsed_partial = _decode_tag(line, "GPU_PROBE_PARTIAL")
                        if parsed_partial is not None:
                            features = parsed_partial
                    if done:
                        break
                elif proc.poll() is not None:
                    break

            if not done and buf:
                parsed_done = _decode_tag(buf, "GPU_PROBE_DONE")
                if parsed_done is not None:
                    features = parsed_done
                    done = True
                elif not features:
                    parsed_partial = _decode_tag(buf, "GPU_PROBE_PARTIAL")
                    if parsed_partial is not None:
                        features = parsed_partial

            renderer = str(features.get("renderer", "")).strip()
            if not features and not renderer:
                if proc.poll() is None:
                    return "", {}, "Process timed out during GPU/feature probe"
                return "", {}, "Could not parse probe output"
            return renderer, features, None
        except Exception as e:
            return "", {}, str(e)
        finally:
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=2)
                except Exception:
                    pass
                try:
                    if proc.stderr:
                        proc.stderr.close()
                except Exception:
                    pass
            shutil.rmtree(tmpdir, ignore_errors=True)

    def probe_gpu_status(self):
        """Launches a headless probe to query the active GPU renderer in runtime.
        Returns:
            tuple: (status: str, renderer: str)
            status can be: 'HARDWARE' | 'SOFTWARE' | 'DISABLED' | 'ERROR' | 'TIMEOUT' | 'NOT_FOUND'
        """
        renderer, features, err = self.probe_features()
        if err:
            if "not found" in err.lower():
                return "NOT_FOUND", err
            if "timed out" in err.lower():
                return "TIMEOUT", err
            return "ERROR", err

        if not features:
            return "UNKNOWN", err or "Could not parse probe output"
        if renderer == "DISABLED" or not features.get("webgl", True):
            return "DISABLED", "Hardware acceleration / WebGL is disabled by Chromium"
        if any(s in renderer for s in ["SwiftShader", "Software", "llvmpipe"]):
            return "SOFTWARE", renderer
        if renderer.startswith("ERROR:"):
            return "ERROR", renderer
        return "HARDWARE", renderer

    def get_launcher_flags(self):
        """Return injected GPU switches found in the launcher binary."""
        launcher_dst = self.get_launcher_dst()
        if not os.path.exists(launcher_dst):
            return []

        try:
            res = subprocess.run(["strings", launcher_dst], capture_output=True, text=True, timeout=10)
        except Exception:
            return []

        prefixes = (
            "--use-angle", "--use-dynamic-angle", "--ignore-gpu-blocklist",
            "--disable-", "--enable-", "--ui-", "--chromium-gpu-patch-profile",
            "chromium-gpu-patch-default-profile=",
        )
        flags = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith(prefixes) and line not in flags:
                flags.append(line)
        return flags

    def diagnose(self, version, skip_patches=None):
        """Read-only diagnostics: launcher flags, pattern matches, feature probe."""
        if skip_patches is None:
            skip_patches = set(self.default_skip_patches)
        print(f"\n{'='*60}")
        print(f" [{self.name}] Diagnostics")
        print(f" Target : {self.app_path}")
        print(f" Version: {version}")
        print(f"{'='*60}")

        raw_flags = self.get_launcher_flags()
        default_profile_marker = None
        flags = []
        for flag in raw_flags:
            if flag.startswith("chromium-gpu-patch-default-profile="):
                default_profile_marker = flag.split("=", 1)[1].strip()
            else:
                flags.append(flag)

        profile = "stock/unknown"
        for flag in flags:
            if flag.startswith(GPU_PATCH_MARKER):
                val = flag.split("=", 1)[1].strip()
                if val:
                    profile = val
                elif default_profile_marker:
                    profile = f"v2 (default: {default_profile_marker})"
                else:
                    profile = "v2 (runtime-selectable)"
                break
        dynamic_angle = "--use-dynamic-angle" in flags
        print(f"[*] Launcher profile: {profile} (dynamic ANGLE: {'yes' if dynamic_angle else 'no'})")
        if flags:
            print("[*] Injected launcher flags:")
            for flag in flags:
                print(f"    {flag}")
        else:
            print("[*] No injected GPU flags found (stock launcher or unsupported binary).")

        print("\n[*] Binary pattern dry-run:")
        pattern_ok = self.apply_binary_patches(version, dry_run=True, skip_patches=skip_patches)

        print("\n[*] Runtime feature probe:")
        renderer, features, err = self.probe_features()
        if err:
            print(f"[!] Probe error: {err}")
        else:
            print(f"    Renderer      : {renderer or 'unknown'}")
            print(f"    WebGL / 2D    : webgl={features.get('webgl')}, canvas2d={features.get('canvas2d')}, webgpu={features.get('webgpuAdapter')}")
            print(f"    Media capture : getUserMedia={features.get('userMedia')}, getDisplayMedia={features.get('displayMedia')}")
            media = features.get("media")
            if isinstance(media, dict) and media:
                codec_bits = []
                for cname in ("h264", "hevc", "vp9", "av1"):
                    info = media.get(cname)
                    if isinstance(info, dict):
                        sup = "yes" if info.get("supported") else "no"
                        pe = "hw" if info.get("powerEfficient") else "sw"
                        codec_bits.append(f"{cname}={sup}({pe})")
                if codec_bits:
                    print(f"    Video decode  : {', '.join(codec_bits)}")
            try:
                import json
                print("    Full probe:")
                print(json.dumps(features, indent=6, sort_keys=True))
            except Exception:
                print(f"    {features}")

        print(f"\n[*] Diagnostic result: required binary patterns "
              f"{'OK' if pattern_ok else 'MISSING/FAILED'}.")
        return 0 if pattern_ok else 1

    def run_patch(self, auto=False, notify_user=False, check_only=False, restore_mode=False, force=False, test_gpu=False, fix_permissions=False, auto_yes=False, gpu_profile=None, dry_run=False, diagnose_only=False, skip_patches=None):
        if not self.is_installed():
            print(f"[-] {self.name} is not installed at {self.app_path}.")
            return 1

        effective_profile = self.default_gpu_profile if gpu_profile is None else gpu_profile
        if effective_profile not in GPU_PROFILES:
            print(f"[!] Unknown GPU profile '{effective_profile}', using {self.default_gpu_profile}.")
            effective_profile = self.default_gpu_profile
        self.gpu_profile = effective_profile
        self.skip_patches = set(self.default_skip_patches) if skip_patches is None else set(skip_patches)

        if fix_permissions:
            print(f"\n{'='*60}")
            print(f" [{self.name}] Target: {self.app_path}")
            print(f" Action: Inject macOS Privacy Permissions (Camera & Mic)")
            print(f"{'='*60}")
            ok = self.grant_privacy_permissions(auto_yes=auto_yes)
            return 0 if ok else 1

        current_ver = self.get_current_version()
        if not current_ver:
            print(f"[!] Error: Could not determine {self.name} version.")
            return 1

        print(f"\n{'='*60}")
        print(f" [{self.name}] Target: {self.app_path} (v{current_ver})")
        print(f"{'='*60}")

        if restore_mode:
            success = self.restore(current_ver)
            return 0 if success else 1

        if diagnose_only:
            return self.diagnose(current_ver, skip_patches=self.skip_patches)

        if dry_run:
            skip_msg = f", skip patches: {sorted(self.skip_patches)}" if self.skip_patches else ""
            print(f"[*] Dry-run mode: no files will be modified (profile: {self.gpu_profile}{skip_msg}).")
            return 0 if self.apply_binary_patches(current_ver, dry_run=True, skip_patches=self.skip_patches) else 1

        if test_gpu:
            print(f"[*] Running headless WebGL hardware acceleration probe for {self.name}...")
            status, info = self.probe_gpu_status()
            if status == "HARDWARE":
                print(f"[+] GPU Status: [HARDWARE ACCELERATED]")
                print(f"    Renderer  : {info}")
                return 0
            elif status == "SOFTWARE":
                print(f"[!] GPU Status: [SOFTWARE FALLBACK] (CPU rendering)")
                print(f"    Renderer  : {info}")
                return 1
            elif status == "DISABLED":
                print(f"[-] GPU Status: [DISABLED] (Chromium context lost / disabled GPU)")
                print(f"    Details   : {info}")
                return 1
            else:
                print(f"[!] GPU Status: [{status}]")
                print(f"    Details   : {info}")
                return 1

        if check_only:
            is_patched = self.is_already_patched(current_ver)
            if is_patched:
                print(f"[+] {self.name} {current_ver} is ALREADY patched.")
            else:
                print(f"[-] {self.name} {current_ver} is NOT patched.")
            return 0 if is_patched else 1

        if auto:
            time.sleep(3)
            current_ver = self.get_current_version()

        if not force:
            if self.is_already_patched(current_ver):
                print(f"[+] {self.name} {current_ver} is ALREADY patched. Nothing to do (use --force to re-apply).")
                return 0
            print(f"[!] Unpatched {self.name} detected ({current_ver})! Starting patch...")

        # 0. Ensure Code Signing Certificate
        self.ensure_certificate()

        # 1. Backup
        self.backup_original(current_ver)

        # 2. Pre-patch hook
        self.pre_patch_hook(current_ver)

        # 3. Binary patching
        ok = self.apply_binary_patches(current_ver, skip_patches=self.skip_patches)
        if not ok:
            print(f"[!] Binary patching failed for {self.name}. Rolling back to backup...")
            self.restore(current_ver)
            return 1

        # 4. Install launcher
        if not self.install_launcher(current_ver):
            print(f"[!] Launcher installation failed for {self.name}. Rolling back...")
            self.restore(current_ver)
            return 1

        # 5. Post-patch hook
        self.post_patch_hook(current_ver)

        # 6. Codesign
        self.codesign(current_ver)

        # 7. Verification
        sig_ok = self.verify_signature()

        if sig_ok:
            if notify_user or auto:
                notify(f"{self.name} GPU Patch", f"{self.name} {current_ver} has been patched successfully for your AMD GPU!")
            return 0
        else:
            print(f"[!] Codesign check returned warnings for {self.name}.")
            return 0
