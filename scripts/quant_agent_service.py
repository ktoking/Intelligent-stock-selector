#!/usr/bin/env python3
"""Install/uninstall the current project as a macOS user LaunchAgent.

No secret is placed in plist/logs. Linux systemd recipe is documented separately.
"""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time


ROOT=Path(__file__).resolve().parents[1]
HOME=Path.home()/".local/share/stock-agent/quant-agent"
LABEL="local.stock-agent.quant-supervisor"


def main():
    parser=argparse.ArgumentParser();parser.add_argument("action",choices=["install","status","stop"])
    args=parser.parse_args()
    if sys.platform!="darwin":
        raise SystemExit("Use the documented systemd service on Linux")
    target=f"gui/{os.getuid()}/{LABEL}"
    if args.action=="status":
        subprocess.run(["launchctl","print",target],check=True);return
    if args.action=="stop":
        subprocess.run(["launchctl","bootout",target],check=True);return
    if not (HOME/"config.json").exists():
        raise SystemExit("First run .venv/bin/python -m quant_agent init --discover-account")
    directory=Path.home()/"Library/LaunchAgents";directory.mkdir(exist_ok=True)
    path=directory/(LABEL+".plist")
    old_plist=plistlib.loads(path.read_bytes()) if path.exists() else None
    plist={"Label":LABEL,"ProgramArguments":["/usr/bin/caffeinate","-i",str(ROOT/".venv/bin/python"),"-m","quant_agent","serve"],
           "WorkingDirectory":str(ROOT),"RunAtLoad":True,"KeepAlive":True,"ThrottleInterval":30,
           "StandardOutPath":str(HOME/"service.stdout.log"),"StandardErrorPath":str(HOME/"service.stderr.log"),
           "EnvironmentVariables":{"PATH":"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}}
    with path.open("wb") as f:plistlib.dump(plist,f)
    path.chmod(0o600)
    existing=subprocess.run(["launchctl","print",target],capture_output=True,text=True)
    same_registration=existing.returncode==0 and old_plist==plist and '/usr/bin/caffeinate' in existing.stdout
    if same_registration and 'state = exiting' not in existing.stdout:
        # Same launch configuration; kickstart restarts the Python code without
        # racing launchd's asynchronous teardown/bootstrap registration.
        subprocess.run(["launchctl","kickstart","-k",target],check=True)
    else:
        if existing.returncode==0:
            result=subprocess.run(['launchctl','bootout',target],capture_output=True,text=True)
            # launchd keeps a retiring job visible for a while. Do not kickstart
            # that old registration (error 37) or mistake it for the new config.
            for _ in range(120):
                if subprocess.run(['launchctl','print',target],capture_output=True).returncode!=0:break
                time.sleep(.25)
            else:raise RuntimeError('LaunchAgent teardown did not finish within 30 seconds')
        for attempt in range(5):
            result=subprocess.run(["launchctl","bootstrap",f"gui/{os.getuid()}",str(path)],capture_output=True,text=True)
            if result.returncode==0:
                break
            registered=subprocess.run(["launchctl","print",target],capture_output=True,text=True)
            if registered.returncode==0 and '/usr/bin/caffeinate' in registered.stdout and 'state = exiting' not in registered.stdout:
                subprocess.run(["launchctl","kickstart","-k",target],check=True);break
            if attempt==4:
                raise RuntimeError("LaunchAgent bootstrap failed: "+result.stderr.strip())
            time.sleep(.5)
    print("Installed:",LABEL,"Status: http://127.0.0.1:8791")


if __name__=="__main__":main()
