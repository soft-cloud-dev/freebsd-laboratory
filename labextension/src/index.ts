import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import type { ILabShell } from '@jupyterlab/application';
import { ICommandPalette, ToolbarButton } from '@jupyterlab/apputils';
import { URLExt } from '@jupyterlab/coreutils';
import {
  INotebookTracker,
  NotebookActions,
  NotebookPanel
} from '@jupyterlab/notebook';
import { ServerConnection } from '@jupyterlab/services';
import { Widget } from '@lumino/widgets';

const PLUGIN_ID = '@softcloud/freebsd-laboratory:plugin';
const EXPORT_COMMAND = 'freebsd-laboratory:export-evidence';
const SHOW_PROGRESS_COMMAND = 'freebsd-laboratory:show-progression';
const JSON_HEADERS = { 'Content-Type': 'application/json' };

interface StageState {
  id: string;
  label: string;
  completed: boolean;
}

interface LaboratoryState {
  schema: string;
  lab: {
    id: string;
    title: string;
    notebook: string | null;
  };
  runtime: {
    system: string;
    release: string;
    machine: string;
    is_freebsd: boolean;
  };
  evidence: {
    session_id: string;
    events: number;
    attestation: string;
  };
  stages: StageState[];
}

interface ExportResult {
  session_id: string;
  path: string;
  files: string[];
}

interface CellDocument {
  cell_type?: string;
  source?: string | string[];
  execution_count?: number | null;
  outputs?: unknown[];
}

const serverSettings = ServerConnection.makeSettings();

function endpoint(path: string): string {
  return URLExt.join(serverSettings.baseUrl, 'freebsd-lab', 'api', path);
}

async function requestJson<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const response = await ServerConnection.makeRequest(
    endpoint(path),
    init,
    serverSettings
  );
  if (!response.ok) {
    throw new Error(
      `FreeBSD Laboratory API ${response.status}: ${await response.text()}`
    );
  }
  return (await response.json()) as T;
}

interface AIUsageState {
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  requests: number;
  tokens_per_second: number;
}

async function fetchAIUsage(): Promise<AIUsageState> {
  const response = await requestJson<{ ok: boolean; usage: AIUsageState }>('ai/usage', {
    method: 'GET'
  });
  return response.usage;
}

async function postEvent(
  kind: string,
  payload: Record<string, unknown>
): Promise<LaboratoryState> {
  const response = await requestJson<{ state: LaboratoryState }>('events', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ kind, payload })
  });
  return response.state;
}

function normalizedSource(document: CellDocument): string {
  if (Array.isArray(document.source)) {
    return document.source.join('');
  }
  return typeof document.source === 'string' ? document.source : '';
}

function cellEvidence(document: CellDocument): Record<string, unknown> {
  return {
    cell_type: document.cell_type ?? 'unknown',
    source: normalizedSource(document),
    execution_count: document.execution_count ?? null,
    output_count: Array.isArray(document.outputs)
      ? document.outputs.length
      : 0
  };
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

class Masthead extends Widget {
  constructor() {
    super();
    this.id = 'freebsd-laboratory-masthead';
    this.addClass('freebsdLab-Masthead');
    this.node.setAttribute('role', 'banner');
    this.node.setAttribute('aria-label', 'FreeBSD Laboratory');
    this.render();
  }

  private render(): void {
    const left = element('div', 'freebsdLab-MastheadBrand');
    const mark = element('span', 'freebsdLab-BrandMark');
    mark.setAttribute('aria-hidden', 'true');
    mark.innerHTML = [
      '<svg viewBox="0 0 56 56" focusable="false" aria-hidden="true">',
      '<path d="M12 12 2 2l3 17z" fill="#b31b21"/>',
      '<path d="M44 12 54 2l-3 17z" fill="#b31b21"/>',
      '<circle cx="28" cy="29" r="22" fill="#b31b21"/>',
      '<path d="M14 18c6-8 18-10 27-3" fill="none" stroke="#df5b5f" stroke-width="3" stroke-linecap="round" opacity=".72"/>',
      '</svg>'
    ].join('');

    left.append(
      mark,
      element('span', 'freebsdLab-Wordmark', 'FreeBSD.'),
      element('span', 'freebsdLab-BrandDivider'),
      element('span', 'freebsdLab-Tagline', 'The Power To Serve')
    );

    const right = element('div', 'freebsdLab-MastheadJupyter');
    right.append(
      element('div', 'freebsdLab-MastheadTitle', 'JupyterLab'),
      element(
        'div',
        'freebsdLab-MastheadSubtitle',
        'FreeBSD executable documentation'
      )
    );

    this.node.append(left, right);
  }
}

interface LaboratoryStatusBarOptions {
  mode?: 'single-document' | 'multiple-document';
  onToggleMode?: () => void;
}

class LaboratoryStatusBar extends Widget {
  private runtime = 'FreeBSD laboratory';
  private notebook = 'Intro.ipynb';
  private mode: 'single-document' | 'multiple-document' = 'multiple-document';
  private onToggleMode?: () => void;

  constructor(options?: LaboratoryStatusBarOptions) {
    super();
    this.id = 'freebsd-laboratory-status';
    this.addClass('freebsdLab-StatusBar');
    this.node.setAttribute('role', 'status');
    if (options?.mode) {
      this.mode = options.mode;
    }
    this.onToggleMode = options?.onToggleMode;
    this.render();
  }

  setMode(mode: 'single-document' | 'multiple-document'): void {
    if (this.mode === mode) {
      return;
    }
    this.mode = mode;
    this.render();
  }

  setState(state: LaboratoryState): void {
    this.runtime = state.runtime.is_freebsd
      ? `${state.runtime.system} ${state.runtime.release}`
      : `${state.runtime.system} ${state.runtime.release}`.trim();
    this.render();
  }

  setNotebook(path: string): void {
    this.notebook = path.split('/').filter(Boolean).pop() ?? path;
    this.render();
  }

  private render(): void {
    this.node.replaceChildren();

    const left = element('div', 'freebsdLab-StatusLeft');
    const simpleToggle = element('button', 'freebsdLab-StatusSimpleToggle');
    simpleToggle.type = 'button';
    simpleToggle.setAttribute('role', 'switch');
    const isSimple = this.mode === 'single-document';
    simpleToggle.setAttribute('aria-checked', isSimple ? 'true' : 'false');
    simpleToggle.setAttribute(
      'aria-label',
      `Simple interface: ${isSimple ? 'on' : 'off'}`
    );
    simpleToggle.title = `Toggle simple interface (${isSimple ? 'currently on' : 'currently off'})`;
    if (isSimple) {
      simpleToggle.classList.add('is-active');
    }

    const simple = element('span', 'freebsdLab-StatusSimple', 'Simple');
    const toggle = element('span', 'freebsdLab-StatusToggle');
    toggle.setAttribute('aria-hidden', 'true');
    toggle.appendChild(element('span', 'freebsdLab-StatusToggleKnob'));
    simpleToggle.append(simple, toggle);

    if (this.onToggleMode) {
      simpleToggle.addEventListener('click', (event: MouseEvent) => {
        event.preventDefault();
        event.stopPropagation();
        this.onToggleMode?.();
      });
    }

    left.append(
      simpleToggle,
      element('span', 'freebsdLab-StatusMetric', '⊙ 0'),
      element('span', 'freebsdLab-StatusMetric', '$_ 0'),
      element('span', 'freebsdLab-StatusMetric', '◉'),
      element('span', 'freebsdLab-StatusRuntime', this.runtime)
    );

    const center = element('div', 'freebsdLab-StatusBrand');
    center.append(
      element('span', 'freebsdLab-StatusBrandMark', '◈'),
      element('strong', 'freebsdLab-StatusBrandName', 'FreeBSD'),
      element('span', 'freebsdLab-StatusBrandTagline', 'The Power To Serve')
    );

    const right = element('div', 'freebsdLab-StatusRight');
    right.append(
      element('span', 'freebsdLab-StatusMode', 'Mode: Command'),
      element('span', 'freebsdLab-StatusPosition', 'Ln 1, Col 1'),
      element('span', 'freebsdLab-StatusNotebook', this.notebook),
      element('span', 'freebsdLab-StatusBell', '○')
    );

    this.node.append(left, center, right);
  }
}

class AITokenUsageBadge extends Widget {
  private _countSpan: HTMLElement;

  constructor() {
    super();
    this.addClass('freebsdLab-TokenBadge');
    const icon = element('span', 'freebsdLab-TokenBadge-icon', '⚡');
    const text = element('span', 'freebsdLab-TokenBadge-text');
    text.appendChild(document.createTextNode('Tokens: '));
    this._countSpan = element('strong', 'freebsdLab-TokenBadge-count', '0');
    text.appendChild(this._countSpan);
    this.node.appendChild(icon);
    this.node.appendChild(text);
    this.node.title = 'Session AI Token Usage: 0 tokens (Resets on kernel restart)';
  }

  setUsage(usage: AIUsageState): void {
    const total = usage.total_tokens ?? 0;
    const formatted = total.toLocaleString();
    this._countSpan.textContent = formatted;
    if (total > 0) {
      this.addClass('freebsdLab-TokenBadge--active');
      const prompt = (usage.prompt_tokens ?? 0).toLocaleString();
      const comp = (usage.completion_tokens ?? 0).toLocaleString();
      const reqs = usage.requests ?? 0;
      const tps = (usage.tokens_per_second ?? 0).toFixed(1);
      this.node.title = `Session AI Tokens: ${formatted} total (${prompt} prompt + ${comp} generated across ${reqs} request(s), ${tps} tok/s) • Resets on kernel restart`;
    } else {
      this.removeClass('freebsdLab-TokenBadge--active');
      this.node.title = 'Session AI Token Usage: 0 tokens (Resets on kernel restart)';
    }
  }
}

class ProgressionPanel extends Widget {
  private state: LaboratoryState | null = null;
  private notice = '';
  private error = '';

  constructor(private readonly onState: (state: LaboratoryState) => void) {
    super();
    this.id = 'freebsd-laboratory-progression';
    this.title.label = 'Lab progression';
    this.title.caption = 'FreeBSD Laboratory progression and evidence';
    this.addClass('freebsdLab-Progression');
    this.render();
  }

  async refresh(): Promise<void> {
    try {
      this.state = await requestJson<LaboratoryState>('state');
      this.error = '';
      this.onState(this.state);
    } catch (error) {
      this.error = error instanceof Error ? error.message : String(error);
    }
    this.render();
  }

  setState(state: LaboratoryState): void {
    this.state = state;
    this.error = '';
    this.onState(state);
    this.render();
  }

  setNotice(value: string): void {
    this.notice = value;
    this.render();
  }

  private render(): void {
    this.node.replaceChildren();

    const header = element('div', 'freebsdLab-ProgressHeader');
    const headerTitle = element('div', 'freebsdLab-ProgressHeaderTitle');
    headerTitle.append(
      element('span', 'freebsdLab-ProgressHeaderMark', '◇'),
      element('span', undefined, 'Lab progression')
    );
    const close = element('button', 'freebsdLab-ProgressClose', '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'Close Lab progression');
    close.addEventListener('click', () => this.hide());
    header.append(headerTitle, close);
    this.node.appendChild(header);

    if (this.error) {
      this.node.appendChild(element('div', 'freebsdLab-Error', this.error));
      return;
    }

    if (!this.state) {
      this.node.appendChild(
        element('div', 'freebsdLab-Loading', 'Loading laboratory state…')
      );
      return;
    }

    const state = this.state;
    const body = element('div', 'freebsdLab-Body');

    const pathSection = element('section', 'freebsdLab-Section');
    pathSection.appendChild(
      element('div', 'freebsdLab-Eyebrow', 'Execution path')
    );
    pathSection.appendChild(
      element('div', 'freebsdLab-PathTitle', 'Path A · Governed laboratory')
    );
    pathSection.appendChild(
      element(
        'div',
        'freebsdLab-Muted',
        'Browser → notebook container → FreeBSD runtime'
      )
    );
    body.appendChild(pathSection);

    const trustSection = element('section', 'freebsdLab-Section');
    trustSection.appendChild(
      element('div', 'freebsdLab-Eyebrow', 'Trust stages')
    );
    const stageList = element('ol', 'freebsdLab-Stages');
    state.stages.forEach((stage, index) => {
      const row = element('li', 'freebsdLab-Stage');
      if (stage.completed) {
        row.classList.add('is-complete');
      }
      const marker = element(
        'span',
        'freebsdLab-StageMarker',
        stage.completed ? '✓' : String(index + 1)
      );
      row.append(
        marker,
        element('span', 'freebsdLab-StageLabel', stage.label)
      );
      stageList.appendChild(row);
    });
    trustSection.appendChild(stageList);
    body.appendChild(trustSection);

    const facts = element('section', 'freebsdLab-Facts');
    this.appendFact(facts, 'Documentation', state.lab.notebook ?? '—');
    this.appendFact(
      facts,
      'Runtime',
      `${state.runtime.system} ${state.runtime.release}`
    );
    this.appendFact(facts, 'Evidence events', String(state.evidence.events));
    body.appendChild(facts);

    const footer = element('div', 'freebsdLab-Footer');
    footer.appendChild(
      element(
        'span',
        'freebsdLab-Attestation',
        state.evidence.attestation.toUpperCase()
      )
    );
    footer.appendChild(
      element(
        'p',
        'freebsdLab-FooterText',
        'Notebook execution events are self-recorded. Platform verification is a separate trust stage.'
      )
    );
    if (this.notice) {
      footer.appendChild(element('p', 'freebsdLab-Notice', this.notice));
    }

    this.node.append(body, footer);
  }

  private appendFact(parent: HTMLElement, label: string, value: string): void {
    const row = element('div', 'freebsdLab-Fact');
    row.append(
      element('span', 'freebsdLab-FactLabel', label),
      element('span', 'freebsdLab-FactValue', value)
    );
    parent.appendChild(row);
  }
}

function notebookContext(panel: NotebookPanel): Record<string, unknown> {
  const cells = panel.content.widgets;
  return {
    notebook: panel.context.path,
    markdown_cells: cells.filter(cell => cell.model.type === 'markdown').length,
    code_cells: cells.filter(cell => cell.model.type === 'code').length
  };
}

const plugin: JupyterFrontEndPlugin<void> = {
  id: PLUGIN_ID,
  autoStart: true,
  requires: [INotebookTracker],
  optional: [ICommandPalette],
  activate: (
    app: JupyterFrontEnd,
    tracker: INotebookTracker,
    palette: ICommandPalette | null
  ): void => {
    const shell = app.shell as ILabShell;
    const toggleMode = (): void => {
      shell.mode =
        shell.mode === 'single-document'
          ? 'multiple-document'
          : 'single-document';
    };

    const statusBar = new LaboratoryStatusBar({
      mode: shell.mode,
      onToggleMode: toggleMode
    });
    const progression = new ProgressionPanel(state => statusBar.setState(state));
    const masthead = new Masthead();
    const attachedNotebooks = new WeakSet<NotebookPanel>();

    document.body.classList.add('freebsdLab-ReferenceShell');
    shell.mode = 'multiple-document';
    app.shell.add(masthead, 'header', { rank: 0 });
    app.shell.add(progression, 'right', { rank: 900 });
    app.shell.add(statusBar, 'bottom', { rank: 0 });

    shell.modeChanged.connect((_sender, mode) => {
      statusBar.setMode(mode);
    });

    const enforceReferenceShell = (): void => {
      shell.mode = 'multiple-document';
      shell.collapseDown();
      shell.expandLeft();
      shell.expandRight();
      progression.show();
      app.shell.activateById(progression.id);

      for (const widget of shell.widgets('bottom')) {
        if (widget !== statusBar) {
          widget.hide();
        }
      }

      void app.commands.execute('filebrowser:activate').catch(error => {
        console.error('FreeBSD Laboratory file browser activation failed', error);
      });
    };

    app.commands.addCommand(EXPORT_COMMAND, {
      label: 'Export laboratory evidence',
      execute: async () => {
        try {
          const result = await requestJson<ExportResult>('export', {
            method: 'POST',
            headers: JSON_HEADERS,
            body: '{}'
          });
          progression.setNotice(`Evidence exported: ${result.path}`);
          await progression.refresh();
        } catch (error) {
          progression.setNotice(
            `Export failed: ${error instanceof Error ? error.message : String(error)}`
          );
        }
      }
    });

    app.commands.addCommand(SHOW_PROGRESS_COMMAND, {
      label: 'Show Lab progression',
      execute: () => {
        shell.expandRight();
        progression.show();
        app.shell.activateById(progression.id);
      }
    });

    palette?.addItem({ command: EXPORT_COMMAND, category: 'FreeBSD Laboratory' });
    palette?.addItem({
      command: SHOW_PROGRESS_COMMAND,
      category: 'FreeBSD Laboratory'
    });

    const activeTokenBadges = new Set<AITokenUsageBadge>();
    const updateAllTokenBadges = (): void => {
      void fetchAIUsage()
        .then(usage => {
          activeTokenBadges.forEach(badge => badge.setUsage(usage));
        })
        .catch(() => {
          // Ignored if AI endpoint unavailable
        });
    };

    const attachNotebook = async (panel: NotebookPanel): Promise<void> => {
      if (attachedNotebooks.has(panel)) {
        return;
      }
      attachedNotebooks.add(panel);

      await panel.revealed;
      await panel.context.ready;
      panel.addClass('freebsdLab-NotebookPanel');

      const pathBar = new Widget();
      pathBar.addClass('freebsdLab-NotebookPath');
      const updatePath = (path: string): void => {
        pathBar.node.textContent = path.startsWith('/') ? path : `/${path}`;
        statusBar.setNotebook(path);
      };
      updatePath(panel.context.path);
      panel.context.pathChanged.connect((_sender, path) => updatePath(path));
      panel.contentHeader.addWidget(pathBar);

      if (
        panel.context.path.endsWith('Intro.ipynb') &&
        panel.content.widgets.length > 0
      ) {
        panel.content.widgets[0].addClass('freebsdLab-IntroCell');
      }

      const exportButton = new ToolbarButton({
        label: '⇩ Export evidence',
        tooltip: 'Export the server-owned evidence bundle',
        className: 'freebsdLab-ExportButton',
        onClick: () => {
          void app.commands.execute(EXPORT_COMMAND);
        }
      });
      exportButton.addClass('freebsdLab-ExportButton');
      panel.toolbar.insertItem(10, 'freebsd-laboratory-export', exportButton);

      const tokenBadge = new AITokenUsageBadge();
      activeTokenBadges.add(tokenBadge);
      panel.disposed.connect(() => {
        activeTokenBadges.delete(tokenBadge);
      });
      panel.toolbar.insertItem(11, 'freebsd-laboratory-ai-tokens', tokenBadge);

      panel.sessionContext.statusChanged.connect((_sender, status) => {
        if (status === 'restarting' || status === 'idle' || status === 'dead') {
          updateAllTokenBadges();
        }
      });
      updateAllTokenBadges();

      try {
        const state = await postEvent('notebook-context', notebookContext(panel));
        progression.setState(state);
      } catch (error) {
        console.error('FreeBSD Laboratory notebook context failed', error);
      }
    };

    tracker.widgetAdded.connect((_sender, panel) => {
      void attachNotebook(panel);
    });

    void tracker.restored
      .then(() => {
        tracker.forEach(panel => {
          void attachNotebook(panel);
        });
        enforceReferenceShell();
      })
      .catch(error => {
        console.error('FreeBSD Laboratory notebook restoration failed', error);
      });

    void app.restored
      .then(() => {
        enforceReferenceShell();
      })
      .catch(error => {
        console.error('FreeBSD Laboratory shell restoration failed', error);
      });

    NotebookActions.executed.connect((_sender, args) => {
      const panel = tracker.find(candidate => candidate.content === args.notebook);
      if (!panel) {
        return;
      }

      updateAllTokenBadges();

      const cellDocument = args.cell.model.toJSON() as CellDocument;
      void postEvent('cell-executed', {
        notebook: panel.context.path,
        cell_id: args.cell.model.id,
        success: args.success,
        error_present: args.error !== null && args.error !== undefined,
        cell: cellEvidence(cellDocument)
      })
        .then(state => {
          progression.setState(state);
        })
        .catch(error => {
          console.error('FreeBSD Laboratory execution evidence failed', error);
        });
    });

    void progression.refresh();
    shell.expandRight();
    progression.show();
    app.shell.activateById(progression.id);
  }
};

export default plugin;
