/*
 * Copyright (C) 2026 SmartInspector Contributors
 *
 * SI Bridge Plugin for Perfetto UI.
 *
 * Provides interactive frame-level analysis by connecting the Perfetto UI
 * to a local SI Agent instance via WebSocket.
 *
 * Features:
 *   - Area selection tab: select a time range → click "Analyze" → get results
 *   - Command + hotkey: Ctrl+Shift+A to analyze current selection
 *   - Result panel: displays Markdown analysis from SI Agent
 */

import m from 'mithril';
import {PerfettoPlugin} from '../../public/plugin';
import {Trace} from '../../public/trace';
import {AreaSelection, ContentWithLoadingFlag} from '../../public/selection';

// ── State ──────────────────────────────────────────────────────────

interface AnalysisState {
  status: 'idle' | 'analyzing' | 'done' | 'error';
  ts: number;
  dur: number;
  result: string;
  error: string;
  progressStep: string;
  progressDetail: string;
  progressLog: string[];
}

const state: AnalysisState = {
  status: 'idle',
  ts: 0,
  dur: 0,
  result: '',
  error: '',
  progressStep: '',
  progressDetail: '',
  progressLog: [],
};

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
const BRIDGE_URL = 'ws://127.0.0.1:9877/bridge';

// ── WebSocket management ──────────────────────────────────────────

function connectWS(): void {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  // Clear any pending reconnect timer
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  try {
    ws = new WebSocket(BRIDGE_URL);
    ws.onopen = () => {
      console.log('[SI Bridge] Connected to SI Agent');
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'analysis_result') {
          state.status = 'done';
          state.result = msg.payload.analysis || msg.payload || '';
          state.progressStep = '';
          state.progressDetail = '';
          state.progressLog = [];
          m.redraw();
        } else if (msg.type === 'analysis_error') {
          state.status = 'error';
          state.error = msg.payload.error || 'Unknown error';
          state.progressStep = '';
          state.progressDetail = '';
          state.progressLog = [];
          m.redraw();
        } else if (msg.type === 'analysis_progress') {
          state.progressStep = msg.payload.step || '';
          state.progressDetail = msg.payload.detail || '';
          const line = msg.payload.detail || msg.payload.step || '';
          if (line) {
            state.progressLog.push(line);
          }
          m.redraw();
        }
      } catch {
        // ignore non-JSON
      }
    };
    ws.onclose = () => {
      console.log('[SI Bridge] Disconnected, reconnecting in 3s...');
      ws = null;
      reconnectTimer = setTimeout(connectWS, 3000);
    };
    ws.onerror = () => {
      // onclose will fire after this
    };
  } catch {
    ws = null;
  }
}

function sendAnalysis(ts: number, dur: number): void {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    state.status = 'error';
    state.error = 'SI Agent not connected. Is /open running?';
    m.redraw();
    return;
  }

  state.status = 'analyzing';
  state.ts = ts;
  state.dur = dur;
  state.result = '';
  state.error = '';
  state.progressStep = 'started';
  state.progressDetail = 'Sending to SI Agent...';
  state.progressLog = ['Sending to SI Agent...'];
  m.redraw();

  ws.send(JSON.stringify({
    type: 'frame_selected',
    payload: {ts, dur},
  }));
}

// ── Formatting helpers ────────────────────────────────────────────

function formatNs(ns: number): string {
  if (ns >= 1_000_000) return `${(ns / 1_000_000).toFixed(2)}ms`;
  if (ns >= 1_000) return `${(ns / 1_000).toFixed(2)}us`;
  return `${ns}ns`;
}

// ── Area Selection Tab ────────────────────────────────────────────

function renderAreaSelectionTab(selection: AreaSelection): ContentWithLoadingFlag | undefined {
  const start = Number(selection.start);
  const end = Number(selection.end);
  const dur = end - start;

  return {
    isLoading: false,
    content: m('.si-bridge-panel', {
      style: 'padding: 12px; font-family: monospace; font-size: 13px;',
    }, [
      m('h4', {
        style: 'margin: 0 0 8px 0; color: #e0e0e0;',
      }, 'SI Frame Analysis'),

      m('.selection-info', {
        style: 'color: #aaa; margin-bottom: 8px;',
      }, `Selected: ${formatNs(start)} — ${formatNs(end)} (${formatNs(dur)})`),

      m('button', {
        style: [
          'background: #1a73e8',
          'color: white',
          'border: none',
          'padding: 6px 16px',
          'border-radius: 4px',
          'cursor: pointer',
          'font-size: 13px',
          state.status === 'analyzing' ? 'opacity: 0.6; cursor: not-allowed;' : '',
        ].join(';'),
        disabled: state.status === 'analyzing',
        onclick: () => sendAnalysis(start, dur),
      }, state.status === 'analyzing' ? 'Analyzing...' : 'Analyze with SI Agent'),

      // Progress log
      state.status === 'analyzing' && state.progressLog.length > 0
        ? m('.si-progress', {
            style: [
              'margin-top: 8px',
              'padding: 6px 8px',
              'background: #1a1a2e',
              'border: 1px solid #333',
              'border-radius: 4px',
              'color: #4fc3f7',
              'font-size: 12px',
              'white-space: pre-wrap',
              'max-height: 200px',
              'overflow-y: auto',
              'line-height: 1.6',
            ].join(';'),
            oncreate: (vnode: m.VnodeDOM) => {
              (vnode.dom as HTMLElement).scrollTop = (vnode.dom as HTMLElement).scrollHeight;
            },
            onupdate: (vnode: m.VnodeDOM) => {
              (vnode.dom as HTMLElement).scrollTop = (vnode.dom as HTMLElement).scrollHeight;
            },
          }, state.progressLog.join('\n'))
        : undefined,

      // Result area
      state.status === 'error'
        ? m('.si-error', {
            style: 'margin-top: 12px; color: #f44336; white-space: pre-wrap;',
          }, state.error)
        : undefined,

      state.status === 'done' && state.result
        ? m('.si-result', {
            style: [
              'margin-top: 12px',
              'padding: 8px',
              'background: #1e1e1e',
              'border: 1px solid #333',
              'border-radius: 4px',
              'color: #ddd',
              'white-space: pre-wrap',
              'max-height: 400px',
              'overflow-y: auto',
              'line-height: 1.5',
            ].join(';'),
          }, state.result)
        : undefined,

      // Connection status indicator
      m('.si-status', {
        style: 'margin-top: 8px; font-size: 11px; color: #666;',
      }, ws && ws.readyState === WebSocket.OPEN
        ? '\u25cf Connected to SI Agent'
        : '\u25cb SI Agent not connected'),
    ]),
  };
}

// ── Plugin ────────────────────────────────────────────────────────

export default class SIBridgePlugin implements PerfettoPlugin {
  static readonly id = 'com.smartinspector.Bridge';

  async onTraceLoad(trace: Trace): Promise<void> {
    // Connect to SI Agent
    connectWS();

    // Register area selection tab
    trace.selection.registerAreaSelectionTab({
      id: 'si_frame_analysis',
      name: 'SI Frame Analysis',
      render: (selection) => renderAreaSelectionTab(selection),
    });

    // Register keyboard command
    trace.commands.registerCommand({
      id: 'com.smartinspector.Bridge#analyzeSelection',
      name: 'SI Agent: Analyze Selected Area',
      callback: () => {
        const sel = trace.selection.selection;
        if (sel.kind === 'area') {
          const start = Number(sel.start);
          const end = Number(sel.end);
          if (start && end && end > start) {
            sendAnalysis(start, end - start);
          } else {
            alert('Invalid area selection');
          }
        } else {
          alert('Please select an area on the timeline first (drag to select)');
        }
      },
    });

    // Register a sidebar menu item
    trace.sidebar.addMenuItem({
      section: 'current_trace',
      text: 'SI Agent Bridge',
      action: () => {
        // Show a persistent tab with connection info
        const uri = 'com.smartinspector.Bridge#Info';
        trace.tabs.registerTab({
          uri,
          content: {
            render(): m.Children {
              return m('.si-bridge-info', {
                style: 'padding: 16px; font-family: monospace;',
              }, [
                m('h3', 'SI Agent Bridge'),
                m('p', 'Connect your Perfetto UI to SI Agent for interactive frame analysis.'),
                m('p', {style: 'color: #aaa;'}, 'How to use:'),
                m('ol', [
                  m('li', 'Drag to select a time range on the timeline'),
                  m('li', 'Click "SI Frame Analysis" tab in the details panel'),
                  m('li', 'Click "Analyze with SI Agent"'),
                ]),
                m('p', {style: 'color: #666; margin-top: 16px;'}, [
                  'Bridge URL: ',
                  m('code', BRIDGE_URL),
                ]),
                m('p', {style: 'color: #666;'}, [
                  'Status: ',
                  m('span', {
                    style: `color: ${ws && ws.readyState === WebSocket.OPEN ? '#4caf50' : '#f44336'}`,
                  }, ws && ws.readyState === WebSocket.OPEN ? 'Connected' : 'Disconnected'),
                ]),
              ]);
            },
            getTitle(): string {
              return 'SI Bridge';
            },
          },
        });
        trace.tabs.showTab(uri);
      },
    });
  }
}
