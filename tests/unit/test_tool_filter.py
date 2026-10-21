"""Unit tests for the env-var tool filter (_ToolFilter)."""

from garmin_mcp import _ToolFilter


class FakeApp:
    """Minimal stand-in for FastMCP: records which tools get registered."""

    def __init__(self):
        self.registered = []
        self.tool_kwargs = {}

    def tool(self, *args, **kwargs):
        explicit = kwargs.get("name") or (
            args[0] if args and isinstance(args[0], str) else None
        )

        def decorator(fn):
            name = explicit or fn.__name__
            self.registered.append(name)
            self.tool_kwargs[name] = kwargs
            return fn

        return decorator

    def run(self):
        return "ran"


def _register(filt, names):
    """Register one no-op tool per name through the filter."""
    for n in names:
        def fn():
            return None

        fn.__name__ = n
        filt.tool()(fn)


def test_no_filter_registers_all():
    app = FakeApp()
    filt = _ToolFilter(app, set(), set())
    _register(filt, ["get_a", "get_b"])
    assert app.registered == ["get_a", "get_b"]


def test_allowlist_only_registers_listed():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a"}, set())
    _register(filt, ["get_a", "get_b"])
    assert app.registered == ["get_a"]


def test_denylist_skips_listed():
    app = FakeApp()
    filt = _ToolFilter(app, set(), {"get_b"})
    _register(filt, ["get_a", "get_b"])
    assert app.registered == ["get_a"]


def test_allowlist_takes_precedence_over_denylist():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a"}, {"get_a"})
    _register(filt, ["get_a", "get_b"])
    assert app.registered == ["get_a"]


def test_matching_is_case_insensitive():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a"}, set())
    _register(filt, ["GET_A"])
    assert app.registered == ["GET_A"]


def test_unknown_filter_names_flags_typos():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a", "get_typo"}, set())
    _register(filt, ["get_a"])
    assert filt.unknown_filter_names() == ["get_typo"]


def test_explicit_name_kwarg_used_for_matching():
    app = FakeApp()
    filt = _ToolFilter(app, {"real_name"}, set())

    def fn():
        return None

    fn.__name__ = "internal_fn"
    filt.tool(name="real_name")(fn)
    assert app.registered == ["real_name"]
    assert filt.unknown_filter_names() == []


def test_passthrough_to_wrapped_app():
    app = FakeApp()
    filt = _ToolFilter(app, set(), set())
    assert filt.run() == "ran"


def test_explicit_read_only_annotations_are_registered():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a"}, set(), {"get_a"})

    _register(filt, ["get_a"])

    annotations = app.tool_kwargs["get_a"]["annotations"]
    assert annotations.readOnlyHint is True
    assert annotations.destructiveHint is False
    assert annotations.idempotentHint is True
    assert annotations.openWorldHint is True


def test_explicit_write_annotations_are_registered_as_non_destructive():
    """Catch write tools being omitted or mislabeled as read-only/destructive."""
    app = FakeApp()
    filt = _ToolFilter(
        app,
        {"upload_workout"},
        set(),
        set(),
        {"upload_workout"},
    )

    _register(filt, ["upload_workout"])

    annotations = app.tool_kwargs["upload_workout"]["annotations"]
    assert annotations.readOnlyHint is False
    assert annotations.destructiveHint is False
    assert annotations.idempotentHint is False
    assert annotations.openWorldHint is True


def test_unlisted_tools_are_not_marked_read_only():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a"}, set(), set())

    _register(filt, ["get_a"])

    assert "annotations" not in app.tool_kwargs["get_a"]


def test_unknown_read_only_names_flags_typos():
    app = FakeApp()
    filt = _ToolFilter(app, {"get_a"}, set(), {"get_a", "get_typo"})

    _register(filt, ["get_a"])

    assert filt.unknown_read_only_names() == ["get_typo"]
