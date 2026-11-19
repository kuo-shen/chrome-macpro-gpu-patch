/*
 * Shared GPU profile definitions for the native browser launchers.
 *
 * The launcher picks a profile in this order:
 *   1. CHROME_GPU_PROFILE environment variable
 *   2. --chromium-gpu-patch-profile=<name> command-line switch
 *   3. CHROME_GPU_DEFAULT_PROFILE compile-time default (compat)
 *
 * "compat" is the current stable flag set. "accelerated" and "custom" are
 * experimental profiles reserved for the feature-reinstatement work; they are
 * intentionally not selected by default.
 */
#ifndef GPU_PROFILES_H
#define GPU_PROFILES_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define GPU_PATCH_MARKER_SWITCH "--chromium-gpu-patch-profile="
#define GPU_PROFILE_ENV "CHROME_GPU_PROFILE"
#define GPU_DISABLE_FEATURES_ENV "CHROME_GPU_DISABLE_FEATURES"
#define GPU_ENABLE_FEATURES_ENV "CHROME_GPU_ENABLE_FEATURES"
#define GPU_EXTRA_FLAGS_ENV "CHROME_GPU_EXTRA_FLAGS"

#ifndef CHROME_GPU_DEFAULT_PROFILE
#define CHROME_GPU_DEFAULT_PROFILE "compat"
#endif

static const char *const kGpuCompatFlags[] = {
    "--use-angle=gl",
    "--ignore-gpu-blocklist",
    "--disable-features=SkiaGraphite,MediaGmbVideoFramePoolMappableSI,ScreenCaptureKitMacScreen,kScreenCaptureKitMacScreen,ScreenCaptureKitDeviceMac",
    "--disable-zero-copy",
    "--ui-disable-zero-copy",
    "--disable-gpu-memory-buffer-compositor-resources",
    "--disable-gpu-memory-buffer-video-frames",
    "--disable-video-capture-use-gpu-memory-buffer",
    "--disable-accelerated-video-decode",
    "--disable-accelerated-2d-canvas",
    "--disable-partial-raster",
};

static const char *const kGpuAcceleratedFlags[] = {
    "--use-angle=gl",
    "--ignore-gpu-blocklist",
};

static const char *const kGpuCustomFlags[] = {
    "--use-angle=gl",
    "--ignore-gpu-blocklist",
};

typedef struct {
    const char *name;
    const char *const *flags;
    size_t count;
} GpuProfile;

#define GPU_PROFILE(n, a) { n, a, sizeof(a) / sizeof(a[0]) }

static const GpuProfile kGpuProfiles[] = {
    GPU_PROFILE("compat", kGpuCompatFlags),
    GPU_PROFILE("accelerated", kGpuAcceleratedFlags),
    GPU_PROFILE("custom", kGpuCustomFlags),
};

static const GpuProfile *gpu_find_profile(const char *name) {
    size_t count = sizeof(kGpuProfiles) / sizeof(kGpuProfiles[0]);
    if (!name || !*name) {
        return &kGpuProfiles[0];
    }
    for (size_t i = 0; i < count; i++) {
        if (strcmp(kGpuProfiles[i].name, name) == 0) {
            return &kGpuProfiles[i];
        }
    }
    return NULL;
}

static const char *gpu_profile_name_from_env_or_argv(int argc, char *argv[]) {
    const char *env = getenv(GPU_PROFILE_ENV);
    if (env && *env) {
        if (gpu_find_profile(env)) {
            return env;
        }
        fprintf(stderr, "[gpu-patch] Ignoring unknown %s=%s; using %s.\n",
                GPU_PROFILE_ENV, env, CHROME_GPU_DEFAULT_PROFILE);
    }

    size_t marker_len = strlen(GPU_PATCH_MARKER_SWITCH);
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], GPU_PATCH_MARKER_SWITCH, marker_len) == 0) {
            const char *value = argv[i] + marker_len;
            if (gpu_find_profile(value)) {
                return value;
            }
            fprintf(stderr, "[gpu-patch] Ignoring unknown profile '%s'; using %s.\n",
                    value, CHROME_GPU_DEFAULT_PROFILE);
        }
    }

    return CHROME_GPU_DEFAULT_PROFILE;
}

static size_t gpu_switch_name_len(const char *arg) {
    if (!arg || arg[0] != '-' || arg[1] != '-') {
        return 0;
    }
    if (strcmp(arg, "--") == 0) {
        return 0;
    }
    return strcspn(arg, "=");
}

static int gpu_has_switch(int argc, char *argv[], const char *switch_name) {
    size_t name_len = strlen(switch_name);
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], switch_name, name_len) == 0 &&
            (argv[i][name_len] == '=' || argv[i][name_len] == '\0')) {
            return 1;
        }
    }
    return 0;
}

static char *gpu_strdup(const char *src) {
    size_t len = strlen(src) + 1;
    char *dst = (char *)malloc(len);
    if (!dst) {
        return NULL;
    }
    memcpy(dst, src, len);
    return dst;
}

typedef struct {
    char *buf;
    size_t len;
    size_t cap;
} GpuCsv;

static int gpu_csv_append(GpuCsv *csv, const char *item, size_t item_len) {
    if (item_len == 0) {
        return 0;
    }
    size_t needed = csv->len + (csv->len ? 1 : 0) + item_len + 1;
    if (needed > csv->cap) {
        size_t next = csv->cap ? csv->cap * 2 : 64;
        while (next < needed) {
            next *= 2;
        }
        char *grown = (char *)realloc(csv->buf, next);
        if (!grown) {
            return -1;
        }
        csv->buf = grown;
        csv->cap = next;
    }
    if (csv->len) {
        csv->buf[csv->len++] = ',';
    }
    memcpy(csv->buf + csv->len, item, item_len);
    csv->len += item_len;
    csv->buf[csv->len] = '\0';
    return 0;
}

static int gpu_csv_contains(const GpuCsv *csv, const char *item, size_t item_len) {
    if (!csv->buf || item_len == 0) {
        return 0;
    }
    const char *p = csv->buf;
    const char *end = csv->buf + csv->len;
    while (p < end) {
        const char *comma = memchr(p, ',', (size_t)(end - p));
        size_t len = comma ? (size_t)(comma - p) : (size_t)(end - p);
        if (len == item_len && strncmp(p, item, len) == 0) {
            return 1;
        }
        if (!comma) {
            break;
        }
        p = comma + 1;
    }
    return 0;
}

static int gpu_csv_add(GpuCsv *csv, const char *values) {
    if (!values) {
        return 0;
    }
    const char *p = values;
    while (*p) {
        const char *comma = strchr(p, ',');
        size_t len = comma ? (size_t)(comma - p) : strlen(p);
        if (len && !gpu_csv_contains(csv, p, len)) {
            if (gpu_csv_append(csv, p, len) != 0) {
                return -1;
            }
        }
        if (!comma) {
            break;
        }
        p = comma + 1;
    }
    return 0;
}

static int gpu_append_flag(const char ***out, int *count, const char *flag) {
    char *copy = gpu_strdup(flag);
    if (!copy) {
        return -1;
    }
    (*out)[(*count)++] = copy;
    return 0;
}

/*
 * Build a new argv with the selected profile's flags merged with any
 * user-supplied feature switches. Returns the new argc, or -1 on failure.
 * The returned argv is heap-allocated and intentionally not freed because
 * the launcher exits immediately after ChromeMain returns.
 */
static int gpu_build_argv(const char *profile_name, int argc, char *argv[],
                          const char ***out_argv) {
    const GpuProfile *profile = gpu_find_profile(profile_name);
    if (!profile) {
        profile = &kGpuProfiles[0];
    }

    /*
     * Split CHROME_GPU_EXTRA_FLAGS up front so feature switches in it are
     * merged together with the profile and user values.
     */
    char *extra_copy = NULL;
    char **extra_tokens = NULL;
    int extra_count = 0;
    const char *extra = getenv(GPU_EXTRA_FLAGS_ENV);
    if (extra && *extra) {
        extra_copy = gpu_strdup(extra);
        if (!extra_copy) {
            return -1;
        }
        extra_tokens = (char **)calloc(strlen(extra) / 2 + 2, sizeof(char *));
        if (!extra_tokens) {
            free(extra_copy);
            return -1;
        }
        char *saveptr = NULL;
        for (char *tok = strtok_r(extra_copy, " \t", &saveptr); tok;
             tok = strtok_r(NULL, " \t", &saveptr)) {
            extra_tokens[extra_count++] = tok;
        }
    }

    const char *profile_disable = "";
    const char *profile_enable = "";
    for (size_t i = 0; i < profile->count; i++) {
        const char *flag = profile->flags[i];
        if (strncmp(flag, "--disable-features=", 19) == 0) {
            profile_disable = flag + 19;
        } else if (strncmp(flag, "--enable-features=", 18) == 0) {
            profile_enable = flag + 18;
        }
    }

    size_t capacity = (size_t)argc + profile->count + (size_t)extra_count + 16;
    const char **new_argv = (const char **)calloc(capacity + 2, sizeof(char *));
    if (!new_argv) {
        free(extra_copy);
        free(extra_tokens);
        return -1;
    }

    int count = 0;
    new_argv[count++] = gpu_strdup(argv[0] ? argv[0] : "");
    if (!new_argv[count - 1]) {
        free(extra_copy);
        free(extra_tokens);
        free(new_argv);
        return -1;
    }

    char marker[256];
    snprintf(marker, sizeof(marker), "%s%s", GPU_PATCH_MARKER_SWITCH, profile->name);
    if (gpu_append_flag(&new_argv, &count, marker) != 0) {
        free(extra_copy);
        free(extra_tokens);
        return -1;
    }

    /*
     * Merge --disable-features / --enable-features from the profile,
     * environment, extra flags, and the user's command line. Unknown
     * duplicates are removed.
     */
    const char *env_disable = getenv(GPU_DISABLE_FEATURES_ENV);
    const char *env_enable = getenv(GPU_ENABLE_FEATURES_ENV);
    GpuCsv disable_csv = {0};
    GpuCsv enable_csv = {0};

    if (gpu_csv_add(&disable_csv, profile_disable) != 0 ||
        gpu_csv_add(&disable_csv, env_disable) != 0 ||
        gpu_csv_add(&enable_csv, profile_enable) != 0 ||
        gpu_csv_add(&enable_csv, env_enable) != 0) {
        free(disable_csv.buf);
        free(enable_csv.buf);
        free(extra_copy);
        free(extra_tokens);
        return -1;
    }

    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "--disable-features=", 19) == 0) {
            if (gpu_csv_add(&disable_csv, argv[i] + 19) != 0) {
                goto merge_fail;
            }
        } else if (strncmp(argv[i], "--enable-features=", 18) == 0) {
            if (gpu_csv_add(&enable_csv, argv[i] + 18) != 0) {
                goto merge_fail;
            }
        }
    }

    for (int i = 0; i < extra_count; i++) {
        if (strncmp(extra_tokens[i], "--disable-features=", 19) == 0) {
            if (gpu_csv_add(&disable_csv, extra_tokens[i] + 19) != 0) {
                goto merge_fail;
            }
        } else if (strncmp(extra_tokens[i], "--enable-features=", 18) == 0) {
            if (gpu_csv_add(&enable_csv, extra_tokens[i] + 18) != 0) {
                goto merge_fail;
            }
        }
    }

    if (disable_csv.buf && disable_csv.len) {
        size_t total = 19 + disable_csv.len + 1;
        char *flag = (char *)malloc(total);
        if (!flag) {
            goto merge_fail;
        }
        snprintf(flag, total, "--disable-features=%s", disable_csv.buf);
        new_argv[count++] = flag;
    }
    if (enable_csv.buf && enable_csv.len) {
        size_t total = 18 + enable_csv.len + 1;
        char *flag = (char *)malloc(total);
        if (!flag) {
            goto merge_fail;
        }
        snprintf(flag, total, "--enable-features=%s", enable_csv.buf);
        new_argv[count++] = flag;
    }

    free(disable_csv.buf);
    free(enable_csv.buf);

    /* Add all non-feature profile flags, and extra flags if not user-supplied. */
    for (size_t i = 0; i < profile->count; i++) {
        const char *flag = profile->flags[i];
        if (strncmp(flag, "--disable-features=", 19) == 0 ||
            strncmp(flag, "--enable-features=", 18) == 0) {
            continue;
        }
        size_t name_len = gpu_switch_name_len(flag);
        char name[128];
        if (name_len >= sizeof(name)) {
            name_len = sizeof(name) - 1;
        }
        memcpy(name, flag, name_len);
        name[name_len] = '\0';
        if (gpu_has_switch(argc, argv, name)) {
            continue; /* user override wins */
        }
        if (gpu_append_flag(&new_argv, &count, flag) != 0) {
            free(extra_copy);
            free(extra_tokens);
            return -1;
        }
    }

    for (int i = 0; i < extra_count; i++) {
        const char *flag = extra_tokens[i];
        if (strncmp(flag, "--disable-features=", 19) == 0 ||
            strncmp(flag, "--enable-features=", 18) == 0) {
            continue;
        }
        size_t name_len = gpu_switch_name_len(flag);
        char name[128];
        if (name_len >= sizeof(name)) {
            name_len = sizeof(name) - 1;
        }
        memcpy(name, flag, name_len);
        name[name_len] = '\0';
        if (name_len && gpu_has_switch(argc, argv, name)) {
            continue;
        }
        if (gpu_append_flag(&new_argv, &count, flag) != 0) {
            free(extra_copy);
            free(extra_tokens);
            return -1;
        }
    }

    /*
     * Keep user arguments except switches that were merged or superseded.
     * The canonical profile marker is always injected above.
     */
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (strncmp(arg, GPU_PATCH_MARKER_SWITCH, strlen(GPU_PATCH_MARKER_SWITCH)) == 0) {
            continue;
        }
        if (strncmp(arg, "--disable-features=", 19) == 0 ||
            strncmp(arg, "--enable-features=", 18) == 0) {
            continue;
        }
        if (gpu_append_flag(&new_argv, &count, arg) != 0) {
            free(extra_copy);
            free(extra_tokens);
            return -1;
        }
    }

    new_argv[count] = NULL;
    free(extra_copy);
    free(extra_tokens);
    *out_argv = new_argv;
    return count;

merge_fail:
    free(disable_csv.buf);
    free(enable_csv.buf);
    free(extra_copy);
    free(extra_tokens);
    return -1;
}

#endif /* GPU_PROFILES_H */
