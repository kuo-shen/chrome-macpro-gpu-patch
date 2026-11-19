#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <libgen.h>
#include <mach-o/dyld.h>
#include <dlfcn.h>
#include "gpu_profiles.h"

int main(int argc, char *argv[]) {
    char exec_path[1024];
    uint32_t size = sizeof(exec_path);
    if (_NSGetExecutablePath(exec_path, &size) != 0) return 1;

    char *dir = dirname(exec_path);
    char fw_path[1024];
    snprintf(fw_path, sizeof(fw_path), 
        "%s/../Frameworks/CocCoc Framework.framework/CocCoc Framework", dir);

    void *lib = dlopen(fw_path, RTLD_LAZY | RTLD_LOCAL | RTLD_FIRST);
    if (!lib) {
        fprintf(stderr, "Failed to dlopen %s: %s\n", fw_path, dlerror());
        return 1;
    }

    int (*chrome_main)(int, const char **) = (int (*)(int, const char **))dlsym(lib, "ChromeMain");
    if (!chrome_main) {
        fprintf(stderr, "Failed to dlsym ChromeMain: %s\n", dlerror());
        return 1;
    }

    /* Select the compat/accelerated/custom profile and merge user flags. */
    const char *profile_name = gpu_profile_name_from_env_or_argv(argc, argv);
    const char **new_argv = NULL;
    int new_argc = gpu_build_argv(profile_name, argc, argv, &new_argv);
    if (new_argc < 0 || !new_argv) {
        fprintf(stderr, "Failed to build GPU profile arguments\n");
        return 1;
    }

    exit(chrome_main(new_argc, new_argv));
}
