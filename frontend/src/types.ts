export type Candidate = {
  code: string;
  name: string;
  action_group: string;
  hit_strategies: string[];
  strategy_consensus: number;
  strategy_total: number;
  mainline: string;
  related_themes: string[];
  sector_strength: number;
  entry_mode: string;
  conclusion: string;
  confirmation: string;
  invalidation: string;
  position: string;
  position_cap_pct: number;
  confidence_grade: string;
  expected_return_pct: number;
  expected_excess_return_pct: number;
  evidence?: {
    rule_reasons?: string[];
    penalty_reasons?: string[];
    enhancements?: Record<string, number>;
    metrics?: Record<string, number>;
    data_completeness?: number;
  };
};

export type RealtimeRow = {
  code: string;
  name: string;
  last_price: number;
  open_price: number;
  pre_close: number;
  pct_chg: number;
  open_gap_pct: number;
  confirm_status: string;
  reason: string;
  entry_mode: string;
  entry_mode_text: string;
  received_at: string;
  is_stale: boolean;
};

export type RealtimeData = {
  trade_date: string;
  market_date: string;
  generated_at: string;
  cache_age_seconds?: number;
  status: string;
  rows: RealtimeRow[];
  refresh_policy: {
    auto_refresh: boolean;
    interval_seconds: number;
    session_label: string;
    market_date: string;
  };
};

export type WorkbenchData = {
  trade_date: string;
  available_dates: string[];
  generated_at: string;
  data_status: string;
  data_completeness: number;
  market_brief: string;
  market: {
    regime_label: string;
    emotion_phase: string;
    market_score: number;
    limit_up_count: number;
    limit_down_count: number;
    broken_rate: number;
    amount_yuan: number;
    position_scale: number;
    risk_flags: string[];
  };
  groups: Record<string, Candidate[]>;
  decision_summary: {
    total: number;
    actionable: number;
    focus: number;
    watch: number;
    avoid: number;
  };
  quick_links: Array<{ label: string; href: string }>;
};

export type IntelligenceData = {
  trade_date: string;
  generated_at: string;
  data_status: string;
  mainlines: Array<{
    name: string;
    candidate_count: number;
    focus_count: number;
    leader_count: number;
    strength: number;
    stocks: Array<{ code: string; name: string }>;
  }>;
  limitup: {
    limit_up_count: number;
    limit_down_count: number;
    max_board_height: number;
    echelon: Array<{
      board_height: number;
      count: number;
      stocks: Array<{ code: string; name: string; pct_chg: number; first_time: string; open_times: number }>;
    }>;
  };
  leaders: {
    counts: Record<string, number>;
    role_counts: Record<string, number>;
    status: string;
    rows: Array<{
      code: string;
      name: string;
      pool_type: string;
      primary_role: string;
      leader_roles: string[];
      leader_score: number;
      lifecycle_state: string;
      leader_time_label: string;
      primary_sector: string;
      pct_chg: number;
      action: string;
    }>;
  };
  lhb: {
    status: string;
    summary: Record<string, number>;
    stocks: Array<{
      code: string;
      name: string;
      pct_chg: number;
      net_buy_yuan: number;
      institution_net_yuan: number;
    }>;
    actors: Array<{ name: string; net_buy_yuan: number; stock_count: number }>;
  };
};

export type StockWorkspace = {
  code: string;
  name: string;
  trade_date: string;
  industry: string[];
  concepts: string[];
  mainline: string;
  daily?: {
    open: number;
    high: number;
    low: number;
    close: number;
    pct_chg: number;
    amount_yuan: number;
  };
  candles: Array<{
    trade_date: string;
    open: number;
    high: number;
    low: number;
    close: number;
    volume: number;
    amount: number;
  }>;
};
