# Running the two web apps without a terminal

Both apps used to be started from a shell, which made them children of that shell. Close the
terminal and they died — four times in one week. Ollama never died, because Ollama installs
itself to start on its own. These tasks do the same thing for the two apps.

## Install

```powershell
powershell -ExecutionPolicy Bypass -File service\install.ps1
```

Registers two scheduled tasks, starts them, and waits until both answer:

| Task | Pipeline | URL |
|---|---|---|
| `ExtractionInvoiceApp` | invoice | http://127.0.0.1:8000 |
| `ExtractionAgreementApp` | agreement | http://127.0.0.1:8100 |

They start again at every logon. A crash is retried three times, a minute apart.

## Check on them

```powershell
Get-ScheduledTask ExtractionInvoiceApp, ExtractionAgreementApp | Format-Table TaskName, State
```

Output goes to `logs\invoice-server.log` and `logs\agreement-server.log`. They also appear in
Task Scheduler under the root folder, where you can stop or disable either one by hand.

## Restart one after changing its code

```powershell
Restart-ScheduledTask -TaskName ExtractionAgreementApp
```

## Remove

```powershell
powershell -ExecutionPolicy Bypass -File service\uninstall.ps1 -Pipeline both
```

`-Pipeline invoice` or `-Pipeline agreement` removes only that one and leaves the other
running.

## Why scheduled tasks and not real Windows services

A Windows service needs an entry point that speaks the service control protocol, or a
third-party wrapper such as NSSM to fake one. Neither buys anything here: these apps listen
on localhost for one logged-in person, so starting at logon is the right lifetime. A
scheduled task is built into Windows, stores no password, and is visible somewhere the user
already knows to look. The problem being solved was *"the app dies when the terminal closes"*,
and a task is not a child of any terminal.

## Keeping the pipelines separable

Two tasks and two `.cmd` files, never one of each. Removing the agreement pipeline entirely
is three independent deletions, none of which touches the invoice app:

```powershell
powershell -File service\uninstall.ps1 -Pipeline agreement
Remove-Item -Recurse -Force agreement_extractor
Remove-Item service\agreement-server.cmd
```

`agreement_extractor/tests/test_separation.py` enforces this: the invoice launcher may not
mention `agreement_extractor`, `.venv313`, port 8100 or `scripts.serve`, and both PowerShell
scripts must accept a `-Pipeline` argument.

The two apps also need different interpreters, which is the other reason they cannot share a
launcher — PaddlePaddle has no wheel for the invoice venv's Python 3.14, so the agreement app
runs on `.venv313`. Its launcher also sets `FLAGS_use_mkldnn=0` *before* Python starts,
because paddle reads that at import; with oneDNN enabled every page OCRs to nothing while the
app looks perfectly healthy.
