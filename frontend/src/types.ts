export type Candidate = {
  code: string;
  name: string;
  action_group: string;
  hit_strategies: string[];
  strategy_consensus: number;
  strategy_total: number;
  mainline: string;
  mainline_confirmed: boolean;
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
  theme_cluster?: string;
  theme_candidate_count?: number;
  theme_ratio_pct?: number;
  crowding_level?: string;
  crowding_note?: string;
  evidence?: {
    rule_reasons?: string[];
    penalty_reasons?: string[];
    exclusion_reasons?: string[];
    failed_evidence?: string[];
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
  market_context: {
    available?: boolean;
    indices?: Array<{ name: string; close: number; pct: number }>;
    up_count?: number | null;
    down_count?: number | null;
    flat_count?: number | null;
    vol_word?: string;
    vol_pct?: number | null;
    amount_text?: string;
  promotion?: {
    overall?: number | null;
    rate_1to2?: number | null;
    rate_2to3?: number | null;
    rate_3to4?: number | null;
    rate_high?: number | null;
  };
  promotion_trend?: {
    score?: number | null;
    label?: string;
    slope?: number | null;
    sample_days?: number;
    tier_scores?: Record<string, number | null>;
    tier_slopes?: Record<string, number | null>;
    history?: Array<{
      trade_date: string;
      rate_1to2?: number | null;
      rate_2to3?: number | null;
      rate_3to4?: number | null;
      rate_high?: number | null;
      rate_1to2_sample?: number;
      rate_2to3_sample?: number;
      rate_3to4_sample?: number;
      rate_high_sample?: number;
    }>;
  };
  profit_effect?: {
    score?: number | null;
    label?: string;
    trend?: string;
    change_3d?: number | null;
    up_ratio?: number | null;
    median_pct?: number | null;
    prev_limit_up_premium?: number | null;
    prev_limit_up_positive?: number | null;
    promotion_rate?: number | null;
    promotion_success?: number | null;
    promotion_sample?: number | null;
    broken_rate?: number | null;
    components?: {
      breadth?: number | null;
      premium?: number | null;
      continuation?: number | null;
      safety?: number | null;
    };
  };
};
  groups: Record<string, Candidate[]>;
  decision_summary: {
    total: number;
    actionable: number;
    focus: number;
    watch: number;
    avoid: number;
  };
  crowding_summary?: Array<{
    cluster: string;
    count: number;
    ratio_pct: number;
    level: string;
  }>;
  cluster_limits?: {
    focus_per_cluster: number;
    active_per_cluster: number;
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
