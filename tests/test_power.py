"""
Tests for build/_power.py — opting the Studio's processes out of Windows
power throttling.

WHAT THIS GUARDS
----------------
  • The process tree is walked from the root the engine names, every
    descendant once, and nothing outside it — a stale parent id that
    points back into the tree (a reused pid) cannot make it loop.
  • Off Windows it does nothing and says why, instead of raising.
  • On Windows, a process can opt itself out, and the setting reads
    back as set. That half runs only on Windows; it is the only real
    check of the ctypes declarations, so run the suite there.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import _power  # noqa: E402


class TestDescendants(unittest.TestCase):
    def test_walks_the_whole_tree_and_nothing_else(self):
        parents = {
            1: 0,
            10: 1,            # the root the engine names (Node)
            11: 10, 12: 10,   # the Python worker, Chromium
            13: 12, 14: 12,   # Chromium's own children
            15: 13,
            20: 1, 21: 20,    # unrelated processes
        }
        self.assertEqual(_power.descendants(parents, 10), [10, 11, 12, 13, 14, 15])

    def test_a_leaf_is_just_itself(self):
        self.assertEqual(_power.descendants({5: 1, 6: 1}, 5), [5])

    def test_an_unknown_root_is_just_itself(self):
        self.assertEqual(_power.descendants({5: 1}, 99), [99])

    def test_a_cycle_from_reused_pids_ends(self):
        # 7's parent id names 9, which is 7's own descendant.
        self.assertEqual(_power.descendants({7: 9, 8: 7, 9: 8}, 7), [7, 8, 9])

    def test_a_process_listed_as_its_own_parent_is_not_its_child(self):
        # The System Idle Process reports pid 0 with parent 0.
        self.assertEqual(_power.descendants({0: 0, 4: 0}, 0), [0, 4])

    def test_an_orphan_of_a_reused_pid_is_not_a_child(self):
        # 30 was started by an earlier process 10 that has exited; the
        # pid 10 now belongs to the engine's Node server, which is
        # younger than 30. Windows still lists 10 as 30's parent.
        parents = {10: 1, 11: 10, 30: 10, 31: 30}
        created = {1: 0, 10: 500, 11: 600, 30: 100, 31: 150}.get
        self.assertEqual(_power.descendants(parents, 10, created), [10, 11])

    def test_an_unknown_creation_time_trusts_the_edge(self):
        # A process that could not be opened is not dropped on a guess.
        parents = {10: 1, 11: 10, 12: 10}
        created = {10: 500, 11: 600}.get
        self.assertEqual(_power.descendants(parents, 10, created), [10, 11, 12])

    def test_a_child_created_in_the_same_tick_is_a_child(self):
        self.assertEqual(_power.descendants({10: 1, 11: 10}, 10, {10: 5, 11: 5}.get),
                         [10, 11])


@unittest.skipIf(sys.platform == "win32", "the no-op path is for other platforms")
class TestOffWindows(unittest.TestCase):
    def test_does_nothing_and_says_why(self):
        r = _power.opt_out_tree(os.getpid())
        self.assertFalse(r["supported"])
        self.assertEqual(r["applied"], [])
        self.assertIn("not Windows", r["reason"])


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class TestOnWindows(unittest.TestCase):
    def _state(self, pid):
        """This process's power-throttling state, read back from Windows."""
        import ctypes
        from ctypes import wintypes

        class State(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD]
        k.GetProcessInformation.restype = wintypes.BOOL
        k.GetCurrentProcess.restype = wintypes.HANDLE
        s = State(_power.PROCESS_POWER_THROTTLING_CURRENT_VERSION, 0, 0)
        ok = k.GetProcessInformation(k.GetCurrentProcess(),
                                     _power.PROCESS_INFORMATION_CLASS_POWER_THROTTLING,
                                     ctypes.byref(s), ctypes.sizeof(s))
        if not ok:
            self.skipTest(f"GetProcessInformation unavailable (error {ctypes.get_last_error()})")
        return s

    def test_a_process_can_opt_itself_out(self):
        r = _power.opt_out_tree(os.getpid())
        self.assertTrue(r["supported"], r)
        self.assertIn(os.getpid(), r["applied"], r)
        s = self._state(os.getpid())
        self.assertTrue(s.ControlMask & _power.PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
                        "the execution-speed policy is now controlled")
        self.assertFalse(s.StateMask & _power.PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
                         "and set to never throttle")


if __name__ == "__main__":
    unittest.main()
