"""One-shot ownership initialization of the fixed application data volume."""
import os


def initialize(root="/data"):
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(root_fd, 10001, 10001)
        for name in ("dev-repo", "dev-repo-runs", "dev-previews", "agent-desktops", "home", "cache"):
            try:
                os.mkdir(name, dir_fd=root_fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                os.fchown(child, 10001, 10001)
            finally:
                os.close(child)
    finally:
        os.close(root_fd)


if __name__ == "__main__":
    initialize()
