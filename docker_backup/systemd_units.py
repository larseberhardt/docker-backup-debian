"""Per-stack systemd timer: OnCalendar mapping + drop-in management.

install.sh installs the template units (docker-backup@.service/.timer); per stack
a drop-in provides the concrete OnCalendar expression.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from . import util

_DOW = {
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}


def systemd_dir() -> str:
    return os.environ.get("DOCKER_BACKUP_SYSTEMD_DIR", "/etc/systemd/system")


def service_path() -> str:
    return os.path.join(systemd_dir(), "docker-backup@.service")


def timer_path() -> str:
    return os.path.join(systemd_dir(), "docker-backup@.timer")


def dropin_dir(name: str) -> str:
    return os.path.join(systemd_dir(), "docker-backup@%s.timer.d" % name)


def dropin_path(name: str) -> str:
    return os.path.join(dropin_dir(name), "schedule.conf")


def _find_time(parts, default="03:00") -> str:
    for p in parts:
        m = re.match(r"^(\d{1,2}):(\d{1,2})$", p)
        if m:
            return "%02d:%02d" % (int(m.group(1)), int(m.group(2)))
    return default


def oncalendar_from(freq: str) -> str:
    """Translates the frequency input into an OnCalendar expression.

    Examples:
      'daily 03:00'      -> '*-*-* 03:00:00'
      'weekly Mon 04:30' -> 'Mon *-*-* 04:30:00'
      'monthly 02:00'    -> '*-*-01 02:00:00'
      'hourly'           -> '*-*-* *:00:00'
      'custom <expr>'    -> '<expr>' (passed through 1:1)
    """
    s = (freq or "").strip()
    if not s:
        return "*-*-* 03:00:00"
    low = s.lower()
    if low.startswith("custom "):
        return s[len("custom "):].strip()

    parts = low.split()
    kind = parts[0]

    if kind == "daily":
        return "*-*-* %s:00" % _find_time(parts)
    if kind == "hourly":
        return "*-*-* *:00:00"
    if kind == "weekly":
        dow = "Mon"
        for p in parts[1:]:
            if p[:3] in _DOW:
                dow = _DOW[p[:3]]
                break
        return "%s *-*-* %s:00" % (dow, _find_time(parts))
    if kind == "monthly":
        return "*-*-01 %s:00" % _find_time(parts)

    # already an OnCalendar expression → unchanged
    return s


def render_dropin(oncalendar: str, randomized_delay_sec: int = 300) -> str:
    # This value is written into a root-owned systemd unit.  Keep validation at
    # the final sink as well as at CLI/manifest boundaries: an embedded newline
    # would otherwise create arbitrary additional unit directives.
    if (not isinstance(oncalendar, str) or not oncalendar.strip()
            or len(oncalendar) > 512
            or any(c in oncalendar for c in ("\r", "\n", "\0"))):
        raise ValueError("Unsafe OnCalendar value: %r" % oncalendar)
    if (not isinstance(randomized_delay_sec, int)
            or isinstance(randomized_delay_sec, bool)
            or not 0 <= randomized_delay_sec <= 86400):
        raise ValueError("RandomizedDelaySec must be an integer between 0 and 86400")
    # The first (empty) line clears any inherited value, then the real one.
    return (
        "[Timer]\n"
        "OnCalendar=\n"
        "OnCalendar=%s\n"
        "RandomizedDelaySec=%d\n"
    ) % (oncalendar, randomized_delay_sec)


def write_schedule_dropin(name: str, oncalendar: str, randomized_delay_sec: int = 300) -> str:
    d = dropin_dir(name)
    os.makedirs(d, exist_ok=True)
    path = dropin_path(name)
    with open(path, "w") as f:
        f.write(render_dropin(oncalendar, randomized_delay_sec))
    return path


def validate_oncalendar(expr: str) -> bool:
    try:
        util.run(["systemd-analyze", "calendar", expr], capture=True, check=True)
        return True
    except util.CommandError:
        return False


def daemon_reload() -> None:
    util.run(["systemctl", "daemon-reload"], capture=False, mutating=True, check=True)


def enable_timer(name: str) -> None:
    util.run(["systemctl", "enable", "--now", "docker-backup@%s.timer" % name],
             capture=False, mutating=True)


def disable_timer(name: str) -> None:
    util.run(["systemctl", "disable", "--now", "docker-backup@%s.timer" % name],
             capture=False, mutating=True, check=True)


def timer_active(name: str) -> Optional[str]:
    try:
        proc = util.run(["systemctl", "is-active", "docker-backup@%s.timer" % name],
                        capture=True, check=False)
        return (proc.stdout or "").strip() or None
    except util.CommandError:
        return None


def timer_enabled(name: str) -> Optional[str]:
    try:
        proc = util.run(["systemctl", "is-enabled", "docker-backup@%s.timer" % name],
                        capture=True, check=False)
        return (proc.stdout or "").strip() or None
    except util.CommandError:
        return None


def timer_next(name: str) -> Optional[str]:
    try:
        proc = util.run(
            ["systemctl", "show", "-p", "NextElapseUSecRealtime", "--value",
             "docker-backup@%s.timer" % name],
            capture=True, check=False,
        )
        return (proc.stdout or "").strip() or None
    except util.CommandError:
        return None
