"""Cross-platform launcher for the complete main app and judge workspace."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import webbrowser

import desktop_entry
import downloadable_demo
import install_update
import operations_server
import browser_proposals


def data_directory(main=False):
    return desktop_entry.private_root() / ('main-workspace' if main else 'judge-workspace')


def prepare(main, directory, port):
    if not main:
        return downloadable_demo.prepare_demo(directory, port=port)
    directory = desktop_entry.workspace_directory(directory, 'main')
    _, version = install_update.payload(Path(__file__).resolve().parent)
    # Constructing a client makes no model request. The same browser reviews and
    # caps apply to owner-configured access and privately entered session keys.
    key = os.environ.get('ORKESTRYN2_NEBIUS_API_KEY', '').strip()
    providers = browser_proposals.providers(key) if key else None
    server = operations_server.Server(directory, port=port, proposal_providers=providers)
    return server, None, version


def open_workspace(server, guide):
    url = guide.as_uri() if guide else 'http://127.0.0.1:' + str(server.server_port)
    try:
        return webbrowser.open(url)
    except (OSError, webbrowser.Error):
        return False


def console(args):
    directory = args.directory or (desktop_entry.private_root() / 'checks' if args.check else data_directory(args.main))
    if args.check:
        result = downloadable_demo.check_demo(directory)
        print(json.dumps(result), flush=True)
        return 0 if result['status'] == 'PASSED' else 1
    server, guide, version = prepare(args.main, directory, args.port)
    print('Orkestryn ' + version, flush=True)
    print('Workspace: http://127.0.0.1:' + str(server.server_port), flush=True)
    if guide:
        print('Private welcome page: ' + str(guide), flush=True)
    print('Private data: ' + str(Path(directory).absolute()), flush=True)
    print('Startup made zero model requests. Keep this launcher running.', flush=True)
    print('Stop the observer and owned workers in Monitoring before closing this launcher.', flush=True)
    try:
        if not args.no_browser:
            open_workspace(server, guide)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def graphical(args):
    import tkinter as tk
    from tkinter import ttk, messagebox
    window = tk.Tk()
    window.title('Orkestryn 2.0')
    window.geometry('620x400')
    window.minsize(530, 360)
    frame = ttk.Frame(window, padding=28)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Orkestryn 2.0', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Agents in sync. Humans in control.').pack(anchor='w', pady=(2, 20))
    status = tk.StringVar(value='Choose a workspace. The complete dashboard opens in your browser.')
    ttk.Label(frame, textvariable=status, wraplength=550).pack(anchor='w', pady=(0, 18))
    state = {'server': None, 'guide': None, 'thread': None}

    def start(main):
        try:
            server, guide, version = prepare(main, args.directory or data_directory(main), args.port)
        except (ValueError, OSError, KeyError):
            messagebox.showerror('Could not start', 'Use a writable private data folder and a complete verified download. No credentials were printed.')
            return
        state.update(server=server, guide=guide)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        state['thread'] = thread
        thread.start()
        for button in (judge, owner):
            button.configure(state='disabled')
        reopen.configure(state='normal')
        status.set(('Main workspace' if main else 'Judge evaluation') + ' · ' + version + '\nhttp://127.0.0.1:' + str(server.server_port) + '\n\nKeep this window open. Startup made zero model requests.')
        if not args.no_browser:
            open_workspace(server, guide)

    judge = ttk.Button(frame, text='Open judge evaluation', command=lambda: start(False))
    judge.pack(fill='x', pady=4)
    owner = ttk.Button(frame, text='Open main workspace', command=lambda: start(True))
    owner.pack(fill='x', pady=4)
    reopen = ttk.Button(frame, text='Reopen browser', state='disabled', command=lambda: open_workspace(state['server'], state['guide']))
    reopen.pack(fill='x', pady=4)
    ttk.Label(frame, text='Before closing: stop the observer and owned workers in Monitoring.\nJudges use the generated private welcome page. Keys are never bundled.', wraplength=550).pack(anchor='w', pady=(18, 0))

    def close():
        server = state['server']
        if server:
            if not messagebox.askokcancel('Close workspace', 'Stop the observer and owned workers in Monitoring first. Closing stops this web server; it does not approve worker shutdown. Any remaining leased workers expire at their fixed deadline. Close now?'):
                return
            server.shutdown()
            server.server_close()
        window.destroy()

    window.protocol('WM_DELETE_WINDOW', close)
    if args.main:
        window.after(0, lambda: start(True))
    window.mainloop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--main', action='store_true', help='Open the independent Orkestryn 2.0 owner workspace.')
    mode.add_argument('--demo', action='store_true', help='Open the isolated judge demo (portable default).')
    parser.add_argument('--console', action='store_true', help='Run without the optional graphical launcher.')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--port', type=int, default=0)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument('--check', action='store_true', help='Run one finite free local walkthrough and exit.')
    checks.add_argument('--study-check', action='store_true', help='Verify the oversight study fixtures and replay offline, then exit.')
    checks.add_argument('--research-round', type=Path, metavar='NEW_ZIP', help='Run the four offline research experiments and save successes and failures to a new ZIP.')
    checks.add_argument('--research-round-2', type=Path, metavar='NEW_ZIP', help='Replay round 2: permissions, actual harm, matched human returns, stale handoffs and budget transfer.')
    checks.add_argument('--research-round-3', type=Path, metavar='NEW_ZIP', help='Replay workload-sensitive inspection, source-aware handoffs and paired evidence gates.')
    checks.add_argument('--research-round-4', type=Path, metavar='NEW_ZIP', help='Replay checkpoint, receipt and handoff experiments; save all findings.')
    checks.add_argument('--research-time-4', type=Path, metavar='NEW_JSON', help='Measure local inspection times in seven sequential workers.')
    args = parser.parse_args(argv)
    if args.port and not 1 <= args.port <= 65535:
        parser.error('Use a port from 1 to 65535, or 0 to choose a free local port.')
    try:
        if any(x is not None for x in (args.research_round, args.research_round_2, args.research_round_3, args.research_round_4, args.research_time_4)):
            if args.research_time_4 is not None:
                import oversight_timing as experiment
                target=args.research_time_4
            elif args.research_round_4 is not None:
                import oversight_round4 as experiment
                target=args.research_round_4
            elif args.research_round_3 is not None:
                import oversight_round3 as experiment
                target=args.research_round_3
            elif args.research_round_2 is not None:
                import oversight_round2 as experiment
                target=args.research_round_2
            else:
                import oversight_round as experiment
                target=args.research_round
            try:
                result = experiment.save(target)
            except FileExistsError:
                print('Evidence already exists at that path. Choose a new filename to preserve the earlier run.')
                return 1
            print(json.dumps(result))
            return 0
        if args.study_check:
            import oversight_studies as studies
            report = studies.reference_report()
            rows = report['rows']
            passed = (len(report['cases']) == 24 and rows[-1]['harmful'] == rows[-1]['safe'] == 12
                      and rows[-1]['located'] == 12 and rows[-1]['false_alarms'] == 0)
            print(json.dumps({'status': 'PASSED' if passed else 'FAILED', 'protocol': studies.VERSION,
                'protocol_digest': studies.protocol_digest(), 'sample_digest': report['sample_digest'],
                'authored_plans': len(report['cases']), 'reference_curve': rows, 'model_calls': 0,
                'human_responses': 0, 'scope': 'Deterministic replay self-check, not observed model or human performance.'}))
            return 0 if passed else 1
        if args.console or args.check:
            return console(args)
        try:
            import tkinter
        except ImportError:
            return console(args)
        try:
            return graphical(args)
        except tkinter.TclError:
            return console(args)
    except (ValueError, OSError, KeyError):
        print('Orkestryn could not start. Use a complete verified download and a writable private data folder. Credentials were not printed.', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
