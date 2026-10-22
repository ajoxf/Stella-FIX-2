"""Open the terminal in a window of its own, not a browser tab.

The trader double-clicks an icon and the terminal is there. No address bar, no
tab strip, and — on a screen that sends live orders — three things that matter:

- **A tab can be closed by accident.** An app window holds one thing, and
  closing it is deliberate rather than a stray ctrl-W aimed at the tab beside.
- **Extensions do not run here.** `--user-data-dir` puts this window in a
  profile of its own, so nothing installed in the trader's ordinary browsing
  profile is present in the process showing the order-entry screen.
- **The chrome is vertical space.** A tab strip and an address bar are about
  70px, and this desk has spent real effort on window height.

Edge first because it is on every Windows 10 and 11 machine, so nothing extra
ships. A missing app window must never mean a missing terminal: if no Chromium
browser is found the ordinary browser is opened and the trader is told why the
window looks different.
"""

import os
import shutil
import subprocess

APP_FLAG = '--app='

WINDOWS_CANDIDATES = (
    r'Microsoft\Edge\Application\msedge.exe',
    r'Google\Chrome\Application\chrome.exe',
)


def find_browser(env=None, exists=os.path.exists, which=shutil.which):
    """The Chromium browser to host the window, or None."""
    env = os.environ if env is None else env
    roots = [env.get('PROGRAMFILES(X86)'), env.get('PROGRAMFILES'),
             env.get('LOCALAPPDATA')]
    for tail in WINDOWS_CANDIDATES:
        for root in roots:
            if not root:
                continue
            path = os.path.join(root, tail)
            if exists(path):
                return path
    for name in ('microsoft-edge', 'google-chrome', 'chromium'):
        found = which(name)
        if found:
            return found
    return None


def profile_dir(env=None):
    """A browser profile of THIS application's own — not the trader's."""
    env = os.environ if env is None else env
    base = (env.get('LOCALAPPDATA') or env.get('XDG_CACHE_HOME')
            or os.path.join(os.path.expanduser('~'), '.cache'))
    return os.path.join(base, 'NexusFIX', 'window')


def window_command(url, browser=None, env=None):
    """The argv that opens `url` as an app window, or None."""
    browser = browser or find_browser(env=env)
    if not browser:
        return None
    return [
        browser, APP_FLAG + url,
        '--user-data-dir=' + profile_dir(env=env),
        '--no-first-run', '--no-default-browser-check',
        '--disable-features=Translate,PasswordManagerOnboarding',
    ]


def open_window(url, browser=None, env=None, spawn=None, say=print):
    """Open the terminal. Returns True when it got its own window.

    Never raises: a browser that will not start is a cosmetic problem, and the
    terminal is reachable at the URL either way.
    """
    spawn = spawn or subprocess.Popen
    argv = window_command(url, browser=browser, env=env)
    if argv:
        try:
            spawn(argv)
            return True
        except Exception as e:                       # noqa: BLE001
            say(f"[start] could not open the app window ({e}) — "
                f"falling back to the browser")
    else:
        say("[start] no Edge or Chrome found, so the terminal opens in your "
            "ordinary browser")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception as e:                           # noqa: BLE001
        say(f"[start] could not open a browser ({e}) — browse to {url}")
    return False
