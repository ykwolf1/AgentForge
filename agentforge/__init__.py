from importlib.metadata import version as _v


def get_version() -> str:
    try:
        return _v("agentforge")
    except Exception:
        return "unknown"
