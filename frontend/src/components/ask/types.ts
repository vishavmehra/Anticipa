// Response shapes of POST /api/v1/ask and GET /api/v1/ask/suggestions (backend/anticipa/ask/models.py).

export interface AskChartData {
  kind: "bar" | "line";
  title: string;
  x: string;
  y: string;
  x_label: string;
  y_label: string;
  unit: string; // "ratio" | "count" | "mixed"
  data: { x: string; y: number }[];
  source_call: string;
  source: string;
}

export interface AskTableColumn {
  key: string;
  label: string;
  unit?: string | null;
}

export interface AskTableData {
  title: string;
  source_call: string;
  source: string;
  columns: AskTableColumn[];
  rows: Record<string, string | number | null>[];
}

export interface AskSource {
  call_id: string;
  tool: string;
  args: Record<string, unknown>;
  definition: string;
  source: string;
}

export interface AskAnswer {
  answer: string;
  chart: AskChartData | null;
  table: AskTableData | null;
  sources: AskSource[];
  follow_ups: string[];
  grounded: boolean;
  refused: boolean;
  notice: string | null;
  model: string | null;
  cost_usd: number;
  latency_ms: number;
  cached: boolean;
  iterations: number;
  synthetic_banner: string;
}

export interface AskSuggestions {
  suggestions: { question: string; topic: string }[];
}

export interface AskHistoryTurn {
  role: "user" | "assistant";
  content: string;
}
