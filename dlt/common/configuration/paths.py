import os
import tempfile

# dlt settings folder
DOT_DLT = ".dlt"

# dlt data dir is by default not set, see get_dlt_data_dir for details
DLT_DATA_DIR: str = None


def get_dlt_project_dir() -> str:
    """The dlt project dir is the current working directory but may be overridden by DLT_PROJECT_DIR env variable."""
    return os.environ.get("DLT_PROJECT_DIR", ".")


def get_dlt_settings_dir() -> str:
    """Returns a path to dlt settings directory. If not overridden it resides in current working directory

    The name of the setting folder is '.dlt'. The path is current working directory '.' but may be overridden by DLT_PROJECT_DIR env variable.
    """
    return os.path.join(get_dlt_project_dir(), DOT_DLT)


def make_dlt_settings_path(path: str) -> str:
    """Returns path to file in dlt settings folder."""
    return os.path.join(get_dlt_settings_dir(), path)


def get_dlt_data_dir() -> str:
    """Gets default directory where pipelines' data will be stored
    1. in user home directory: ~/.dlt/
    2. if current user is root: in /var/dlt/
    3. if current user does not have a home directory: in /tmp/dlt/
    4. if DLT_DATA_DIR is set in env then it is used
    """
    if "DLT_DATA_DIR" in os.environ:
        return os.environ["DLT_DATA_DIR"]

    # geteuid not available on Windows
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # we are root so use standard /var
        return os.path.join("/var", "dlt")

    home = _get_user_home_dir()
    if home is None:
        # no home dir - use temp
        return os.path.join(tempfile.gettempdir(), "dlt")
    else:
        # if home directory is available use ~/.dlt/pipelines
        return os.path.join(home, DOT_DLT)


def _get_user_home_dir() -> str:
    return os.path.expanduser("~")




def create_symlink_to_dlt(symlink_dir):
    """
    Creates a symbolic link pointing to the '.dlt' directory.

    This function checks if the '.dlt' directory exists in the current working directory.
    If it doesn't exist, the function creates it. Then, it checks if the specified symbolic
    link directory exists. If not, the function creates a symbolic link at the specified
    symlink location pointing to the '.dlt' directory.

    Parameters:
        symlink_dir (str): The path where the symbolic link will be created.

    Note:
        This function is intended for internal use and assumes that the path provided for
        the symlink is valid. If the symbolic link already exists, it won't be recreated.

    Raises:
        OSError: If the symlink creation fails due to operating system-related errors.
    """
    if not os.path.exists(DOT_DLT):
        os.makedirs(DOT_DLT)
    if not os.path.exists(symlink_dir):
        os.symlink(DOT_DLT, symlink_dir)
        print(f"Created a visible symlink: {symlink_dir} -> {DOT_DLT}")

