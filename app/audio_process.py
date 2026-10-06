"""Resource limits applied in a fresh child, never preexec_fn in API threads."""
import os
import resource
import sys

if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    try:
        os.execvp(sys.argv[1], sys.argv[1:])
    except OSError:
        sys.exit(127)
