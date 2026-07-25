#!/usr/bin/env python
"""Drive the UI headlessly and capture screenshots as verification evidence.

Runs against 127.0.0.1 only — nothing is exposed to the network. Waits for
React to actually render before each capture, so the images show real state
rather than the boot placeholder.

    .venv/bin/python scripts/capture_ui.py
"""
import json
import os
import sys
import time
import urllib.request

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE = os.environ.get("MDM_BASE", "http://127.0.0.1:8099")
OUT = os.environ.get("SHOT_DIR", "/tmp/shots")
os.makedirs(OUT, exist_ok=True)


def login(username, password):
    req = urllib.request.Request(
        f"{BASE}/api/v1/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["access_token"]


def driver_for(token, width=1680, height=1150):
    opts = Options()
    opts.add_argument("--headless")
    opts.set_preference("browser.cache.disk.enable", False)
    opts.set_preference("devtools.console.stdout.content", True)
    drv = webdriver.Firefox(service=Service(log_output=os.devnull), options=opts)
    drv.set_window_size(width, height)
    # Set the session cookie on the right origin before loading the SPA.
    drv.get(f"{BASE}/health")
    drv.add_cookie({"name": "mdm_session", "value": token, "path": "/"})
    return drv


def shot(drv, route, name, *, wait_for=None, wait_text=None, actions=None, settle=1.6):
    drv.get(f"{BASE}{route}")
    try:
        WebDriverWait(drv, 25).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        # The boot placeholder must be gone — that means React mounted.
        WebDriverWait(drv, 25).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#root > .boot"))
        )
        if wait_for:
            WebDriverWait(drv, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_for))
            )
        if wait_text:
            WebDriverWait(drv, 25).until(
                lambda d: wait_text.lower() in d.page_source.lower()
            )
    except TimeoutException:
        print(f"  ! timeout waiting on {name}")

    if actions:
        try:
            actions(drv)
        except Exception as exc:
            print(f"  ! action failed for {name}: {exc}")
    time.sleep(settle)

    path = os.path.join(OUT, name)
    drv.save_screenshot(path)

    # Firefox's WebDriver has no browser log endpoint, so surface render
    # failures instead: a stuck boot placeholder or a visible error banner.
    problems = []
    if drv.find_elements(By.CSS_SELECTOR, "#root > .boot"):
        problems.append("React never mounted (boot placeholder still visible)")
    for el in drv.find_elements(By.CSS_SELECTOR, ".banner-err"):
        txt = (el.text or "").strip().replace("\n", " ")
        if txt:
            problems.append(f"error banner: {txt[:120]}")

    size = os.path.getsize(path)
    print(f"  {name:34s} {size:>7,} bytes"
          + ("  OK" if not problems else f"  [{len(problems)} problem(s)]"))
    for p in problems[:3]:
        print("       !", p)
    return path


def click_text(label, tag="button", *, selector=None, optional=False, settle=1.6):
    """Click the first element whose visible text contains `label`."""
    def _do(drv):
        pool = (drv.find_elements(By.CSS_SELECTOR, selector) if selector
                else drv.find_elements(By.TAG_NAME, tag))
        want = label.lower().strip()
        for el in pool:
            try:
                if want in (el.text or "").lower().strip() and el.is_displayed():
                    drv.execute_script("arguments[0].click();", el)
                    time.sleep(settle)
                    return True
            except Exception:
                continue
        if optional:
            return False
        raise RuntimeError(f"no {selector or tag} matching {label!r}")
    return _do


def steps(*fns):
    """Run several actions in sequence."""
    def _do(drv):
        for fn in fns:
            fn(drv)
    return _do


def main():
    # Credentials come from the environment; this is a local dev/CI helper and
    # deliberately ships no default passwords.
    admin_user = os.environ.get("SHOT_ADMIN_USER", "admin")
    steward_user = os.environ.get("SHOT_STEWARD_USER", "steward_kim")
    admin_pw = os.environ.get("SHOT_ADMIN_PW")
    steward_pw = os.environ.get("SHOT_STEWARD_PW")
    if not admin_pw or not steward_pw:
        raise SystemExit(
            "Set SHOT_ADMIN_PW and SHOT_STEWARD_PW (and optionally "
            "SHOT_ADMIN_USER / SHOT_STEWARD_USER) before capturing screenshots."
        )

    print("Authenticating…")
    admin = login(admin_user, admin_pw)
    steward = login(steward_user, steward_pw)

    NAV = ".nav-item"

    print("\nAdministrator perspective:")
    drv = driver_for(admin)
    try:
        shot(drv, "/", "10-admin-overview.png", wait_text="Pipeline health")
        shot(drv, "/models", "11-admin-models.png", wait_for=".card-head",
             wait_text="materialises")
        # DDL preview modal — the label differs for draft vs deployed entities.
        shot(drv, "/models", "12-admin-ddl-preview.png", wait_text="materialises",
             actions=steps(
                 click_text("Sync DDL", optional=True, settle=2.8),
                 click_text("Publish", optional=True, settle=2.8),
             ), settle=2.6)
        shot(drv, "/models", "13-admin-model-detail.png", wait_text="materialises",
             actions=click_text("View", settle=2.2), settle=2.0)
        shot(drv, "/admin", "14-admin-system.png", wait_text="Cluster privileges")
        shot(drv, "/admin", "15-admin-users.png", wait_text="Cluster privileges",
             actions=click_text("Users & roles", selector=".tab", settle=2.0))
        shot(drv, "/admin", "16-admin-ldap.png", wait_text="Cluster privileges",
             actions=click_text("LDAP / AD", selector=".tab", settle=2.0))
        shot(drv, "/admin", "17-admin-audit.png", wait_text="Cluster privileges",
             actions=click_text("Audit log", selector=".tab", settle=2.2))
        shot(drv, "/records", "18-golden-records.png", wait_text="golden records")
    finally:
        drv.quit()

    print("\nData steward perspective:")
    drv = driver_for(steward)
    try:
        shot(drv, "/", "20-steward-overview.png", wait_text="Pipeline health")
        shot(drv, "/review", "21-steward-queue-picker.png", wait_text="Review queues")
        # Scope to table buttons: the sidebar nav also contains the word "Review".
        enter_queue = click_text("Review", selector="td .btn", settle=2.8)
        shot(drv, "/review", "22-steward-queue.png", wait_text="Review queues",
             actions=enter_queue, settle=2.2)
        shot(drv, "/review", "23-steward-review-detail.png", wait_text="Review queues",
             actions=steps(enter_queue, click_text("Open", selector="td .btn", settle=3.0)),
             settle=2.6)
        shot(drv, "/models", "24-steward-models-readonly.png", wait_text="materialises")
    finally:
        drv.quit()

    print(f"\nScreenshots written to {OUT}")


if __name__ == "__main__":
    sys.exit(main())
