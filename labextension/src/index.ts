import {
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
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

async function postEvent(
  kind: string,
  payload: Record<string, unknown>
): Promise<LaboratoryState> {
  const response = await requestJson<{ state: LaboratoryState }>('events', {
    method: 'POST',
    body: JSON.stringify({ kind, payload })
  });
  return response.state;
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

class ProgressionPanel extends Widget {
  private state: LaboratoryState | null = null;
  private notice = '';
  private error = '';

  constructor() {
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
    } catch (error) {
      this.error = error instanceof Error ? error.message : String(error);
    }
    this.render();
  }

  setState(state: LaboratoryState): void {
    this.state = state;
    this.error = '';
    this.render();
  }

  setNotice(value: string): void {
    this.notice = value;
    this.render();
  }

  private render(): void {
    this.node.replaceChildren();

    if (this.error) {
      this.node.appendChild(
        element('div', 'freebsdLab-Error', this.error)
      );
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
    pathSection.appendChild(element('div', 'freebsdLab-Eyebrow', 'Execution path'));
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
    trustSection.appendChild(element('div', 'freebsdLab-Eyebrow', 'Trust stages'));
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
      row.append(marker, element('span', 'freebsdLab-StageLabel', stage.label));
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
      element('span', 'freebsdLab-Attestation', state.evidence.attestation.toUpperCase())
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
  activate: async (
    app: JupyterFrontEnd,
    tracker: INotebookTracker,
    palette: ICommandPalette | null
  ): Promise<void> => {
    const progression = new ProgressionPanel();
    app.shell.add(progression, 'right', { rank: 900 });

    app.commands.addCommand(EXPORT_COMMAND, {
      label: 'Export laboratory evidence',
      execute: async () => {
        try {
          const result = await requestJson<ExportResult>('export', {
            method: 'POST',
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

    palette?.addItem({ command: EXPORT_COMMAND, category: 'FreeBSD Laboratory' });

    const attachNotebook = async (panel: NotebookPanel): Promise<void> => {
      await panel.revealed;
      await panel.context.ready;

      const exportButton = new ToolbarButton({
        label: 'Export evidence',
        tooltip: 'Export the server-owned evidence bundle',
        onClick: () => {
          void app.commands.execute(EXPORT_COMMAND);
        }
      });
      exportButton.addClass('freebsdLab-ExportButton');
      panel.toolbar.insertItem(10, 'freebsd-laboratory-export', exportButton);

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

    await tracker.restored;
    tracker.forEach(panel => {
      void attachNotebook(panel);
    });

    NotebookActions.executed.connect((_sender, args) => {
      const panel = tracker.find(candidate => candidate.content === args.notebook);
      if (!panel) {
        return;
      }

      const cell = args.cell.model.toJSON();
      void postEvent('cell-executed', {
        notebook: panel.context.path,
        cell_id: args.cell.model.id,
        success: args.success,
        error: args.error ? String(args.error) : null,
        cell
      })
        .then(state => progression.setState(state))
        .catch(error => {
          console.error('FreeBSD Laboratory execution evidence failed', error);
        });
    });

    await progression.refresh();
    app.shell.activateById(progression.id);
  }
};

export default plugin;
