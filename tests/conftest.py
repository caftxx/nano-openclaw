import sys

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # The gateway lifecycle test spawns a daemon that shares the console.
    # The daemon's event loop may emit a CTRL_C_EVENT that propagates to
    # ALL console-attached processes, including the pytest runner — even
    # tests that run *after* the lifecycle test.  signal.SIG_IGN is not
    # reliable on Windows (it only sets the Python-level handler, not the
    # Windows console handler).  Use the Win32 API directly to absorb
    # spurious CTRL_C events for the whole session.  CTRL_BREAK (type 1)
    # is left unhandled so the user can still cancel with Ctrl+Break.
    import ctypes

    _HANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    @_HANDLER_ROUTINE
    def _absorb_ctrl_c(ctrl_type):
        if ctrl_type == 0:  # CTRL_C_EVENT
            return 1  # handled — do not propagate
        return 0  # pass through (Ctrl+Break, close, etc.)

    ctypes.windll.kernel32.SetConsoleCtrlHandler(_absorb_ctrl_c, 1)
