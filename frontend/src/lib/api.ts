export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "/api_proxy";
const API_TIMEOUT_MS = 15000;

function alternateLoopbackBase(base: string): string | null {
  if (base.includes("127.0.0.1")) {
    return base.replace("127.0.0.1", "localhost");
  }
  if (base.includes("localhost")) {
    return base.replace("localhost", "127.0.0.1");
  }
  return null;
}

export type Position = {
  id: string;
  security_id: string;
  ticker?: string | null;
  entity_name?: string | null;
  direction: string;
  list_type: string;
  quantity: number;
  avg_cost_basis: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  realized_pnl: number;
  conviction?: number | null;
  added_at: string;
  updated_at?: string;
};

export type Transaction = {
  id: string;
  position_id?: string | null;
  status?: string;
  superseded_by_id?: string | null;
  ticker?: string | null;
  entity_name?: string | null;
  action: string;
  quantity: number;
  price?: number | null;
  executed_at: string;
  notes?: string | null;
  lot_type?: string | null;
  provenance_json?: Record<string, unknown> | null;
  source_type?: string | null;
  source_label?: string | null;
  source_evidence_id?: string | null;
  source_confidence?: number | null;
  provenance?: Record<string, unknown>;
};

export type TransactionCorrectionRequest = {
  action?: string;
  quantity?: number;
  price?: number | null;
  executed_at?: string;
  notes?: string | null;
  lot_type?: string | null;
  reason?: string | null;
};

export type TransactionCorrectionRecord = {
  original: Transaction;
  replacement?: Transaction | null;
  reason?: string | null;
  corrected_at?: string | null;
};

export type ResearchObjectResult = {
  position_id: string;
  profile_id: string;
  coverage_map_id: string;
  ticker: string;
  entity_name: string;
  list_type: string;
  open_question_count: number;
};

export type PortfolioBuildPoint = {
  as_of: string;
  net_capital_deployed: number;
  gross_trade_notional: number;
  active_holding_count: number;
  transaction_count: number;
};

export type PortfolioOverview = {
  holdings: Position[];
  watchlist: Position[];
  considering: Position[];
  recent_transactions: Transaction[];
  top_winners: Position[];
  top_losers: Position[];
  total_value: number;
  buying_power: number;
  build_series: PortfolioBuildPoint[];
};

export type TimelineItem = {
  id: string;
  item_type: "fact" | "claim" | "event";
  text: string;
  tier: string;
  importance?: string | null;
  directness?: string | null;
  novelty?: string | null;
  contradiction_role?: string | null;
  signal_score: number;
  subject_name?: string | null;
  source_name?: string | null;
  source_type?: string | null;
  event_time?: string | null;
  public_time?: string | null;
  ingest_time?: string | null;
  display_time: string;
  display_time_label: string;
  created_at: string;
};

export type KnowledgeChange = {
  id: string;
  change_id?: string | null;
  node_type: string;
  text: string;
  change_type: "created" | "updated" | "deprecated" | string;
  change_source?: "audit_event" | "derived_state" | string;
  changed_at: string;
  created_at: string;
  updated_at: string;
  is_deprecated: boolean;
  deprecated_reason?: string | null;
  superseded_by_id?: string | null;
  reason?: string | null;
  actor?: string | null;
  source_type?: string | null;
  source_id?: string | null;
  subject_type?: string | null;
  subject_id?: string | null;
  metadata?: Record<string, unknown> | null;
};

export type KnowledgeChangeSummary = {
  active_facts: number;
  active_claims: number;
  active_events: number;
  deprecated_facts: number;
  deprecated_claims: number;
  deprecated_events: number;
  changes: KnowledgeChange[];
};

export type GraphCitation = {
  raw_evidence_id: string;
  source_item_id?: string | null;
  source_id: string;
  source_name: string;
  source_type: string;
  source_item_type?: string | null;
  origin_kind?: string | null;
  origin_label?: string | null;
  origin_detail?: string | null;
  layer: string;
  is_system: boolean;
  system_reason?: string | null;
  title?: string | null;
  url?: string | null;
  author?: string | null;
  created_at: string;
};

export type GraphConnection = {
  edge_id: string;
  direction: "incoming" | "outgoing";
  relationship_type: string;
  confidence: number;
  node_id: string;
  node_type: string;
  label: string;
  subtitle?: string | null;
  tier?: string | null;
  created_at?: string | null;
};

export type GraphNodeDetail = {
  id: string;
  node_type: string;
  label: string;
  layer: string;
  body?: string | null;
  tier?: string | null;
  created_at?: string | null;
  relevance?: number | null;
  relevance_reasoning?: string | null;
  properties: Record<string, unknown>;
  citations: GraphCitation[];
  connections: GraphConnection[];
};

export type AgentActionLogItem = {
  id: string;
  timestamp: string;
  source: string;
  action_type: string;
  status: string;
  summary: string;
  subject_id?: string | null;
  subject_type?: string | null;
  subject_name?: string | null;
  metadata: Record<string, unknown>;
};

export type GraphWebNode = {
  key: string;
  id: string;
  node_type: string;
  label: string;
  layer: string;
  subtitle?: string | null;
  tier?: string | null;
  created_at?: string | null;
  is_root: boolean;
  is_autonomous: boolean;
  relevance?: number | null;
  x?: number | null;
  y?: number | null;
  vx?: number | null;
  vy?: number | null;
};

export type GraphNodeLayoutItem = {
  node_key: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
};

export type GraphWebEdge = {
  id: string;
  source_key: string;
  target_key: string;
  relationship_type: string;
  confidence: number;
};

export type GraphNeighborhood = {
  root_key: string;
  depth: number;
  nodes: GraphWebNode[];
  edges: GraphWebEdge[];
};

export type GraphStats = {
  active_facts: number;
  active_claims: number;
  active_events: number;
  deprecated_facts: number;
  deprecated_claims: number;
  deprecated_events: number;
  total_edges: number;
  profiles: number;
  sources: number;
  raw_evidence: number;
  source_items: number;
  fundamental_metrics: number;
  market_setup_signals: number;
  active_knowledge_nodes: number;
  total_knowledge_nodes: number;
};

export type GraphRelation = {
  node_a_key: string;
  node_b_key: string;
  direct_relationships: GraphWebEdge[];
  shared_neighbor_keys: string[];
  summary: string;
  nodes: GraphWebNode[];
  edges: GraphWebEdge[];
};

export type GraphPruneResult = {
  pruned_count: number;
  detail: string;
  proposed_count?: number;
  rejected_count?: number;
  review_required?: boolean;
};

export type ProfileListItem = {
  id: string;
  subject_type: string;
  subject_id: string;
  subject_name: string;
  executive_summary?: string | null;
  current_stance?: string | null;
  confidence_band?: string | null;
  coverage_score?: number | null;
  updated_at: string;
};

export type ProfileDetail = {
  id: string;
  subject_type: string;
  subject_id: string;
  subject_name: string;
  executive_summary?: string | null;
  bull_case?: string | null;
  bear_case?: string | null;
  active_contradictions: string[];
  current_stance?: string | null;
  confidence_band?: string | null;
  current_thesis_summary?: string | null;
  what_would_falsify: string[];
  coverage_score?: number | null;
  missing_evidence: Array<{
    id: string;
    class_name: string;
    importance_to_thesis: string;
    identified_at: string;
  }>;
  unresolved_questions: Array<{
    id: string;
    question_text: string;
    urgency: number;
    status: string;
    created_at: string;
  }>;
  recent_evidence: Array<{
    id: string;
    node_type: string;
    text: string;
    tier?: string | null;
    created_at: string;
  }>;
  historical_analogy_lenses: Array<{
    name: string;
    period?: string | null;
    lens_use_policy?: string | null;
    current_application_prompt?: string | null;
    what_rhymes?: string | null;
    dominant_channel_test?: string | null;
    where_analogy_breaks?: string | null;
    portfolio_transmission?: string | null;
    best_next_check?: string | null;
    investor_questions: string[];
  }>;
  fundamental_metrics: Array<{
    id: string;
    metric_name: string;
    metric_family: string;
    ticker?: string | null;
    value_text?: string | null;
    numeric_value?: number | null;
    unit?: string | null;
    currency?: string | null;
    period_label?: string | null;
    as_of?: string | null;
    public_time?: string | null;
    stale_after?: string | null;
    direction?: string | null;
    confidence: number;
    investment_relevance?: string | null;
    next_test?: string | null;
    freshness_status?: string | null;
    source_name?: string | null;
    source_type?: string | null;
    evidence_title?: string | null;
    url?: string | null;
  }>;
  market_setup_signals: Array<{
    id: string;
    signal_name: string;
    signal_family: string;
    ticker?: string | null;
    setup_context?: string | null;
    actual_context?: string | null;
    price_reaction?: string | null;
    value_text?: string | null;
    numeric_value?: number | null;
    unit?: string | null;
    currency?: string | null;
    period_label?: string | null;
    as_of?: string | null;
    public_time?: string | null;
    direction?: string | null;
    confidence: number;
    investment_relevance?: string | null;
    next_test?: string | null;
    outcome_status?: string | null;
    outcome_score?: number | null;
    outcome_assessment?: Record<string, unknown> | null;
    outcome_assessment_attempt?: {
      attempt_count?: number | null;
      attempted_at?: string | null;
      next_retry_at?: string | null;
      assessment?: string | null;
      confidence?: number | null;
      rationale?: string | null;
      limitations?: string | null;
      recommended_research_query?: string | null;
      research_followup?: {
        started?: boolean | null;
        reason?: string | null;
        evidence_id?: string | null;
        processed?: boolean | null;
        query?: string | null;
        title?: string | null;
      } | null;
    } | null;
    source_name?: string | null;
    source_type?: string | null;
    evidence_title?: string | null;
    url?: string | null;
  }>;
  updated_at: string;
};

export type AutomationStatus = {
  automation_enabled: boolean;
  jobs: Array<{
    name: string;
    enabled: boolean;
    interval_seconds?: number | null;
    last_run_at?: string | null;
    last_status: string;
    detail?: string | null;
  }>;
};

export type OpportunityUniverseMember = {
  id: string;
  security_id: string;
  entity_id: string;
  ticker: string;
  entity_name: string;
  enabled: boolean;
  priority: number;
  source: string;
  origins: OpportunityUniverseOrigin[];
  last_inspected_at?: string | null;
  next_inspection_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type OpportunityUniverseOrigin = {
  source_type: string;
  source_id: string;
  label: string;
  observed_at: string;
  metadata: Record<string, unknown>;
};

export type OpportunityUniverseImportSource =
  | "tracked_positions"
  | "researched_catalog"
  | "benchmark_constituents";

export type OpportunityUniverseImportPreview = {
  captured_at: string;
  source_summaries: Array<{
    source_type: OpportunityUniverseImportSource;
    label: string;
    eligible_count: number;
    missing_count: number;
    existing_count: number;
    skipped_count: number;
  }>;
  candidates: Array<{
    security_id: string;
    entity_id: string;
    ticker: string;
    entity_name: string;
    asset_class: string;
    instrument_type: string;
    status: "missing" | "present";
    origins: OpportunityUniverseOrigin[];
  }>;
  skipped: Array<Record<string, unknown>>;
};

export type OpportunityUniverseImportResult = {
  imported_count: number;
  existing_count: number;
  provenance_updated_count: number;
  member_ids: string[];
  preview: OpportunityUniverseImportPreview;
};

export type OpportunityDiscoveryRun = {
  id: string;
  status: string;
  captured_at: string;
  started_at: string;
  completed_at?: string | null;
  universe_size: number;
  planned_count: number;
  inspected_count: number;
  skipped_count: number;
  failed_count: number;
  estimated_credits: number;
  remaining_member_ids: string[];
  inspected_member_ids: string[];
  skipped: Array<Record<string, unknown>>;
  failures: Array<Record<string, unknown>>;
  provider_attempts: Array<Record<string, unknown>>;
  limits: Record<string, unknown>;
  detail?: string | null;
};

export type OpportunityCandidate = {
  id: string;
  run_id: string;
  entity_id: string;
  security_id: string;
  shadow_experiment_id?: string | null;
  ticker: string;
  status: "new" | "monitoring" | "rejected" | "expired" | "shadow_tested" | string;
  title: string;
  family_key?: string | null;
  priority_score: number;
  signal_stage?: string | null;
  why_now: string;
  investable_thesis: string;
  portfolio_transmission: string;
  expected_edge: string;
  falsification_tests: string[];
  assumptions: string[];
  uncertainties: string[];
  evidence_refs: string[];
  evidence_snapshot: Array<Record<string, unknown>>;
  ranking: Record<string, unknown>;
  review_reason?: string | null;
  captured_at: string;
  first_seen_at: string;
  last_seen_at: string;
  expires_at: string;
  observations: OpportunityCandidateObservation[];
};

export type OpportunityCandidateObservation = {
  id: string;
  run_id: string;
  captured_at: string;
  horizon_label: string;
  horizon_days: number;
  due_at: string;
  expected_relative_direction: "outperform" | "underperform" | "unscored" | string;
  status: "pending" | "evaluated" | string;
  profile_snapshot: Record<string, unknown>;
  evidence_refs: string[];
  evidence_snapshot: Array<Record<string, unknown>>;
  benchmark_ticker: string;
  market_data_provider: string;
  candidate_start_time?: string | null;
  candidate_start_price?: number | null;
  benchmark_start_time?: string | null;
  benchmark_start_price?: number | null;
  evaluated_at?: string | null;
  candidate_end_time?: string | null;
  candidate_end_price?: number | null;
  benchmark_end_time?: string | null;
  benchmark_end_price?: number | null;
  candidate_return_pct?: number | null;
  benchmark_return_pct?: number | null;
  excess_return_pct?: number | null;
  cash_return_pct: number;
  result_label?: "supported" | "challenged" | "inconclusive" | "direction_unrecorded" | string | null;
  attempt_count: number;
  last_attempt_at?: string | null;
  last_error?: string | null;
  evaluation_policy: Record<string, unknown>;
};

export type ShadowExperiment = {
  id: string;
  name: string;
  policy_description: string;
  start_point: string;
  end_point: string;
  trigger_type?: string | null;
  trigger_reason?: string | null;
  horizon_label?: string | null;
  initiated_by?: string | null;
  execution_mode: "autonomous" | "manual" | string;
  operator_prompt?: string | null;
  discovery_profile?: ShadowOpportunityProfile | null;
  guidance_mode?: string | null;
  guidance_summary?: string | null;
  snapshot_summary: {
    holding_count?: number;
    tracked_count?: number;
    total_market_value?: number;
    remaining_buying_power?: number;
  };
  run_details: {
    guidance?: {
      guidance_mode?: string;
      guidance_summary?: string;
      cash_reserve_pct?: number;
      max_position_multiplier?: number;
    };
    starting_buying_power?: number;
    ending_buying_power?: number;
    reserve_target?: number;
    paper_account?: {
      provider?: string;
      cash?: number;
      cash_reserved?: number;
      buying_power?: number;
      market_value?: number;
      equity?: number;
      position_count?: number;
      slippage_bps?: number;
      fee_per_order?: number;
      max_buy_order_pct_equity?: number;
      regular_session_only?: boolean;
    };
    progress?: {
      phase?: string;
      step_count?: number;
      target_steps?: number;
      started_at?: string | null;
      last_updated_at?: string | null;
      next_checkpoint_at?: string | null;
    };
    pending_evidence_events?: ShadowEvidenceWakeup[];
    evidence_event_log?: Array<
      ShadowEvidenceWakeup & {
        consumed_at?: string | null;
        checkpoint_index?: number | null;
      }
    >;
    checkpoint_log?: Array<{
      step_index: number;
      captured_at?: string | null;
      actual_return?: number | null;
      shadow_return?: number | null;
      alpha?: number | null;
      buying_power?: number | null;
      guidance_mode?: string | null;
      summary?: string | null;
      checkpoint_objective?: string | null;
      planned_posture?: string | null;
      research_goal?: string | null;
    }>;
    decision_history?: Array<{
      step_index: number;
      observed_at?: string | null;
      checkpoint_objective?: string | null;
      portfolio_view?: string | null;
      planned_posture?: string | null;
      why_now?: string | null;
      research_goal?: string | null;
      monitoring_focus?: string[];
      what_would_change_mind?: string[];
      prior_realization?: string | null;
      shadow_research?: {
        started?: boolean;
        reason?: string | null;
        title?: string | null;
        query?: string | null;
        processed?: boolean;
        evidence_id?: string | null;
      } | null;
      baseline_comparison?: {
        shadow_return?: number | null;
        real_portfolio_return?: number | null;
        alpha?: number | null;
      };
      decisions?: Array<{
        ticker?: string | null;
        entity_name?: string | null;
        action?: string | null;
        observed_signal?: string | null;
        thesis_view?: string | null;
        expected_outcome?: string | null;
        risk_guardrail?: string | null;
        rationale?: string | null;
      }>;
    }>;
    run_log?: Array<{
      step_index?: number | null;
      observed_at?: string | null;
      ticker: string;
      action: string;
      quantity: number;
      price: number;
      rationale: string;
      post_trade_buying_power: number;
      order_status?: string | null;
      order_rejection_reason?: string | null;
      quote_session?: string | null;
      desired_quantity?: number | null;
      size_adjustments?: string[];
      entity_name?: string | null;
      stance?: string | null;
      confidence_band?: string | null;
      thesis_summary?: string | null;
      actual_market_value?: number | null;
      actual_weight_pct?: number | null;
    }>;
  };
  report: {
    trigger_summary?: {
      trigger_type?: string | null;
      trigger_reason?: string | null;
      initiated_by?: string | null;
      horizon_label?: string | null;
    };
    opportunity_summary?: ShadowOpportunityProfile | null;
    baseline_summary?: {
      holding_count?: number | null;
      total_market_value?: number | null;
      remaining_buying_power?: number | null;
    };
    policy_summary?: {
      policy_description?: string | null;
      operator_prompt?: string | null;
      guidance_mode?: string | null;
      guidance_summary?: string | null;
      objective?: string | null;
    };
    expected_outcome?: {
      summary?: string | null;
      expected_shadow_return?: number | null;
      expected_alpha_vs_baseline?: number | null;
    };
    actual_outcome?: {
      summary?: string | null;
      actual_portfolio_return?: number | null;
      shadow_portfolio_return?: number | null;
      alpha_vs_real_portfolio?: number | null;
      outperformed_baseline?: boolean | null;
    };
    learning_summary?: {
      baseline_used?: string | null;
      baseline_description?: string | null;
      why_this_matters?: string | null;
      lesson_direction?: string | null;
      maturity_status?: string | null;
      confidence_score?: number | null;
      supporting_observations?: number | null;
      contradicting_observations?: number | null;
      neutral_observations?: number | null;
      lesson_id?: string | null;
    };
    outcome_summary?: {
      shadow_return?: number | null;
      actual_return?: number | null;
      alpha?: number | null;
      max_drawdown?: number | null;
      reasoning?: string | null;
    };
    decision_history_summary?: Array<{
      step_index?: number | null;
      checkpoint_objective?: string | null;
      planned_posture?: string | null;
      why_now?: string | null;
      research_goal?: string | null;
      prior_realization?: string | null;
      baseline_comparison?: {
        shadow_return?: number | null;
        real_portfolio_return?: number | null;
        alpha?: number | null;
      };
    }>;
    policy_assessment?: string | null;
    thesis_context?: Array<{
      ticker?: string | null;
      entity_name?: string | null;
      stance?: string | null;
      confidence_band?: string | null;
      thesis_summary?: string | null;
      action?: string | null;
      rationale?: string | null;
    }>;
    key_lesson?: string | null;
    open_questions?: string[];
  };
  run_status: string;
  skip_reason?: string | null;
  created_at: string;
  completed_at?: string | null;
  actions: Array<{
    id: string;
    experiment_id: string;
    action: string;
    security_id: string;
    quantity: number;
    price: number;
    simulated_timestamp: string;
    rationale: string;
  }>;
  orders: Array<{
    id: string;
    experiment_id: string;
    security_id: string;
    ticker: string;
    client_order_id: string;
    provider: string;
    side: string;
    order_type: string;
    time_in_force: string;
    status: string;
    requested_quantity: number;
    filled_quantity: number;
    reference_price: number;
    filled_avg_price?: number | null;
    reserved_notional: number;
    quote_session?: string | null;
    quote_time?: string | null;
    submitted_at: string;
    accepted_at?: string | null;
    filled_at?: string | null;
    canceled_at?: string | null;
    rejection_reason?: string | null;
    rationale: string;
    checkpoint_index: number;
    evidence_refs_json: string[];
    source_decision_json: Record<string, unknown>;
  }>;
  fills: Array<{
    id: string;
    order_id: string;
    experiment_id: string;
    security_id: string;
    side: string;
    quantity: number;
    price: number;
    gross_notional: number;
    fee: number;
    slippage_bps: number;
    filled_at: string;
    quote_time?: string | null;
    quote_session?: string | null;
    cash_after: number;
    position_quantity_after: number;
  }>;
  account_events: Array<{
    id: string;
    experiment_id: string;
    source_transaction_id: string;
    security_id: string;
    ticker: string;
    event_type: string;
    status: string;
    occurred_at: string;
    applied_at: string;
    quantity_before: number;
    quantity_after: number;
    cash_before: number;
    cash_after: number;
    amount: number;
    derivation: string;
    detail?: string | null;
  }>;
  paper_positions: Array<{
    security_id: string;
    ticker: string;
    quantity: number;
    avg_cost_basis: number;
    current_price: number;
    market_value: number;
    unrealized_pnl: number;
    weight_pct: number;
    marked_at?: string | null;
  }>;
  result?: {
    id: string;
    experiment_id: string;
    shadow_return: number;
    actual_return: number;
    alpha: number;
    max_drawdown: number;
    sharpe_ratio?: number | null;
    reasoning: string;
  } | null;
  lesson?: Lesson | null;
};

export type ShadowEvidenceWakeup = {
  event_id: string;
  raw_evidence_id?: string | null;
  subject_type?: string | null;
  subject_id?: string | null;
  security_id?: string | null;
  trigger_reason: string;
  queued_at?: string | null;
  metadata?: {
    triggers?: string[];
    stance?: string | null;
    confidence?: string | null;
    coverage_score?: number | null;
    contradictions?: number | null;
  };
};

export type DashboardSummary = {
  as_of: string;
  holdings_count: number;
  watchlist_count: number;
  considering_count: number;
  total_market_value: number;
  total_unrealized_pnl: number;
  total_value: number;
  buying_power: number;
  top_winners: Position[];
  top_losers: Position[];
  portfolio_build_series: PortfolioBuildPoint[];
  profile_count: number;
  evidence_node_count: number;
  active_evidence_node_count?: number;
  deprecated_evidence_node_count?: number;
  open_questions_count: number;
  pending_shadow_experiments_count: number;
  automation_enabled: boolean;
  jobs: AutomationStatus["jobs"];
  recent_transactions: Array<{
    id: string;
    ticker: string;
    entity_name?: string | null;
    action: string;
    quantity: number;
    price?: number | null;
    executed_at: string;
    source_type?: string | null;
    source_label?: string | null;
    source_evidence_id?: string | null;
    source_confidence?: number | null;
    provenance?: Record<string, unknown>;
  }>;
  recent_evidence: ProfileDetail["recent_evidence"];
  recent_profiles: ProfileListItem[];
  open_questions: Array<{
    id: string;
    subject_type: string;
    subject_name: string;
    question_text: string;
    urgency: number;
    created_at: string;
  }>;
  review_queue: ReviewQueueItem[];
  recent_lessons: Array<{
    id: string;
    title: string;
    summary: string;
    lesson_type: string;
    created_at: string;
  }>;
  trusted_sources: Array<{
    id: string;
    name: string;
    source_type: string;
    is_trusted: boolean;
    updated_at: string;
  }>;
  research_activity: {
    automation_enabled: boolean;
    provider_configured: boolean;
    open_question_count: number;
    pending_evidence_count: number;
    latest_run_at?: string | null;
    latest_status?: string | null;
    latest_detail?: string | null;
    latest_item_title?: string | null;
    latest_item_subject_name?: string | null;
    latest_item_created_at?: string | null;
    latest_item_processed: boolean;
  };
  recent_research_actions: Array<{
    timestamp: string;
    status: string;
    summary: string;
    title?: string | null;
    query?: string | null;
    search_depth?: string | null;
  }>;
  recent_agent_actions: Array<{
    id: string;
    timestamp: string;
    source: string;
    action_type: string;
    status: string;
    summary: string;
    subject_id?: string | null;
    subject_type?: string | null;
    subject_name?: string | null;
    metadata?: Record<string, unknown>;
  }>;
  portfolio_monitor: {
    monitored_holding_count: number;
    priority_review_count: number;
    priority_review_items: Array<{
      item_label: string;
      item_type: string;
      priority_score: number;
      trigger_reason: string;
    }>;
    recent_research_items: Array<{
      id: string;
      title: string;
      subject_name: string;
      created_at: string;
      is_processed: boolean;
    }>;
  };
  recent_shadow_experiments: Array<{
    id: string;
    name: string;
    policy_description: string;
    run_status: string;
    created_at: string;
    completed_at?: string | null;
    alpha?: number | null;
    shadow_return?: number | null;
    actual_return?: number | null;
  }>;
  llm_usage: {
    analysis_runs_24h: number;
    cached_runs_24h: number;
    verification_runs_24h: number;
    total_input_tokens_24h: number;
    total_output_tokens_24h: number;
    avg_duration_ms: number;
  };
  active_benchmark_ticker?: string | null;
  portfolio_return_pct?: number | null;
  benchmark_return_pct?: number | null;
  active_return_pct?: number | null;
  top_sector?: string | null;
  top_sector_weight_pct: number;
  current_regime?: string | null;
};

export type AgentActionLogEntry = {
  id: string;
  timestamp: string;
  source: string;
  action_type: string;
  status: string;
  summary: string;
  subject_id?: string | null;
  subject_type?: string | null;
  subject_name?: string | null;
  metadata?: Record<string, unknown> | null;
};

export type AgentActionLogResponse = {
  actions: AgentActionLogEntry[];
};

export type ActiveWatcher = {
  id: string;
  source?: string | null;
  source_id?: string | null;
  ticker?: string | null;
  entity_id?: string | null;
  condition_type: string;
  condition_params_json?: Record<string, unknown> | null;
  objective: string;
  adjustment_plan?: string | null;
  deadline?: string | null;
  status: string;
  is_active: boolean;
  last_checked_at?: string | null;
  triggered_at?: string | null;
  trigger_detail?: string | null;
  created_at?: string | null;
  countdown_seconds?: number | null;
  is_overdue?: boolean;
  reminder_kind?: "condition" | "deadline" | "deadline_and_condition" | string;
};

export type IntegrationSettings = {
  gmail: {
    enabled: boolean;
    imap_host: string;
    imap_port: number;
    username: string;
    folder: string;
    only_unseen: boolean;
    fetch_limit: number;
    allowed_senders: string[];
    allowed_domains: string[];
    required_subject_keywords: string[];
    password_set: boolean;
    ready: boolean;
    status_message?: string | null;
  };
  plaid: {
    enabled: boolean;
    environment: "sandbox" | "development" | "production" | string;
    client_id_set: boolean;
    secret_set: boolean;
    access_token_set: boolean;
    item_id?: string | null;
    ready: boolean;
    status_message?: string | null;
  };
  market_data: {
    enabled: boolean;
    provider: string;
    refresh_interval_seconds: number;
    ready: boolean;
    status_message?: string | null;
  };
  paper_trading: {
    enabled: boolean;
    provider: "local_simulator" | string;
    slippage_bps: number;
    fee_per_order: number;
    max_buy_order_pct_equity: number;
    allow_fractional: boolean;
    require_regular_session: boolean;
    ready: boolean;
    status_message?: string | null;
  };
  llm: {
    provider: "ollama" | "codex_cli" | "nvidia_nim" | string;
    hosted_base_url: string;
    hosted_model: string;
    available_providers: Array<{
      provider: string;
      label: string;
      is_local: boolean;
      requires_api_key: boolean;
      accepts_model: boolean;
      accepts_base_url: boolean;
      supports_streaming: boolean;
      default_model: string;
      default_base_url: string;
    }>;
    api_key_set: boolean;
    ready: boolean;
    status_message?: string | null;
  };
  research: {
    provider: string;
    provider_order: string[];
    available_providers: Array<{
      provider: string;
      label: string;
      requires_api_key: boolean;
      requires_base_url: boolean;
      is_metered: boolean;
    }>;
    searxng_base_url: string;
    api_key_set: boolean;
    tavily_monthly_credit_budget?: number | null;
    ready: boolean;
    status_message?: string | null;
  };
  opportunity_discovery: {
    enabled: boolean;
    interval_seconds: number;
    max_subjects_per_run: number;
    revisit_hours: number;
    candidate_ttl_days: number;
  };
  portfolio: {
    default_benchmark_ticker: string;
    remaining_buying_power: number;
  };
};

export type ResearchUsageRequestEntry = {
  timestamp: string;
  provider?: string | null;
  query?: string | null;
  title?: string | null;
  search_depth?: string | null;
  status: string;
  result_count?: number | null;
  top_url?: string | null;
  request_id?: string | null;
  error?: string | null;
  metadata: Record<string, unknown>;
  evidence_id?: string | null;
  processed?: boolean | null;
};

export type ResearchUsageSnapshot = {
  provider: string;
  ready: boolean;
  status_message: string;
  key?: string | Record<string, unknown> | null;
  account?: Record<string, unknown> | null;
  recent_requests: ResearchUsageRequestEntry[];
};

export type SetupStatus = {
  status: string;
  completion_ratio: number;
  next_recommended_step?: string | null;
  development_reset_enabled: boolean;
  steps: Array<{
    id: string;
    label: string;
    description: string;
    status: string;
    status_label?: string | null;
    detail?: string | null;
    hint?: string | null;
    action_label?: string | null;
    href?: string | null;
  }>;
};

export type DevelopmentResetResult = {
  ok: boolean;
  detail: string;
  reset_at: string;
  cleared_tables: string[];
  preserved_tables: string[];
  storage_cleared: boolean;
  runtime_settings_reset: boolean;
  warnings: string[];
};

export type SourceRecord = {
  id: string;
  name: string;
  source_type: string;
  url?: string | null;
  description?: string | null;
  is_trusted: boolean;
  origin?: SourceOrigin | null;
  evidence_count: number;
  trust_profile?: {
    factual_reliability?: string | null;
    noise_ratio?: string | null;
    trust_trajectory?: string | null;
    correction_quality?: string | null;
  } | null;
  value_profile?: {
    idea_generation_value?: string | null;
    timing_value?: string | null;
    portfolio_relevance_value?: string | null;
    specificity?: string | null;
    originality?: string | null;
  } | null;
  quality_segments: Array<{
    domain?: string | null;
    ticker?: string | null;
    horizon?: string | null;
    regime?: string | null;
    quality_score: number;
    originality_score: number;
    timing_usefulness: number;
    evidence_count: number;
    notes?: string | null;
  }>;
  performance_history: Array<{
    id: string;
    source_id: string;
    domain?: string | null;
    sector?: string | null;
    regime?: string | null;
    period_start: string;
    period_end: string;
    total_claims: number;
    correct_claims: number;
    incorrect_claims: number;
    accuracy_rate: number;
    originality_rate: number;
    timing_score: number;
    computed_at: string;
  }>;
  claim_queue: {
    total: number;
    pending: number;
    deferred: number;
    assessed: number;
    last_assessment_at?: string | null;
  };
  recent_items: Array<{
    id: string;
    title?: string | null;
    url?: string | null;
    created_at: string;
    source_item_type?: string | null;
    is_processed: boolean;
    origin_kind?: string | null;
    origin_label?: string | null;
    origin_detail?: string | null;
    user_feedback?: Record<string, unknown> | null;
  }>;
  created_at: string;
  updated_at: string;
};

export type SourceOrigin = {
  origin_kind?: string | null;
  origin_label?: string | null;
  origin_detail?: string | null;
};

export type SubjectAliasRecord = {
  id: string;
  alias: string;
  normalized_alias: string;
  subject_type: string;
  subject_id: string;
  subject_name: string;
  source: string;
  confidence: number;
  reason?: string | null;
  linked_symbols: string[];
  created_at: string;
  updated_at: string;
};

export type SubjectAliasSubjectOption = {
  subject_type: string;
  subject_id: string;
  subject_name: string;
  subtitle?: string | null;
  linked_symbols: string[];
  is_active_holding: boolean;
};

export type GraphSearchResult = {
  node_type: string;
  node_id: string;
  label: string;
  subtitle?: string | null;
  layer: string;
  created_at?: string | null;
};

export type SourceEvidenceSummary = {
  id: string;
  source_id: string;
  source_name: string;
  source_type: string;
  title?: string | null;
  url?: string | null;
  source_item_type: string;
  is_processed: boolean;
  origin_kind?: string | null;
  origin_label?: string | null;
  origin_detail?: string | null;
  user_feedback?: {
    rating?: string | null;
    note?: string | null;
    context?: string | null;
    flagged_at?: string | null;
    lesson_id?: string | null;
    lesson_title?: string | null;
  } | null;
  created_at: string;
  updated_at: string;
};

export type SourceEvidenceDetail = SourceEvidenceSummary & {
  author?: string | null;
  external_id?: string | null;
  raw_content_ref?: string | null;
  event_time?: string | null;
  public_time?: string | null;
  ingest_time?: string | null;
  eligible_action_time?: string | null;
  metadata: Record<string, unknown>;
  source_item_summary?: string | null;
  source_item_excerpt?: string | null;
  source_item_processing_status?: string | null;
};

export type MediaIngestionCapability = {
  key: string;
  label: string;
  status: "available" | "not_configured" | "unsupported" | string;
  detail: string;
};

export type MediaIngestionCapabilityResponse = {
  can_extract_without_transcript: boolean;
  current_best_path: string;
  capabilities: MediaIngestionCapability[];
};

export type MediaIngestionJobEvent = {
  phase: string;
  message: string;
  created_at: string;
  detail?: Record<string, unknown> | null;
};

export type MediaIngestionJob = {
  job_id: string;
  status: "queued" | "running" | "completed" | "error" | "cancelled" | string;
  request_url: string;
  created_at: string;
  updated_at: string;
  events: MediaIngestionJobEvent[];
  result?: {
    ok?: boolean;
    error?: string;
    evidence_id?: string;
    transcript_length?: number;
    video_id?: string;
    ingest_mode?: string;
  } | null;
  error?: string | null;
};

export type OwnershipDisclosureCreate = {
  source_name: string;
  source_type: "filing" | "ownership_tracker";
  source_url?: string | null;
  source_description?: string | null;
  source_item_type:
    | "insider_disclosure"
    | "ownership_disclosure"
    | "institutional_flow"
    | "congressional_trade_disclosure";
  title?: string | null;
  url?: string | null;
  external_id?: string | null;
  author?: string | null;
  metadata: Record<string, unknown>;
  summary?: string | null;
  event_time?: string | null;
  public_time?: string | null;
  eligible_action_time?: string | null;
};

export type SourceFeedbackRecord = {
  evidence_id: string;
  source_id: string;
  source_name: string;
  source_type: string;
  title?: string | null;
  url?: string | null;
  source_item_type: string;
  origin_kind?: string | null;
  origin_label?: string | null;
  origin_detail?: string | null;
  rating: string;
  note?: string | null;
  context?: string | null;
  flagged_at?: string | null;
  lesson_id?: string | null;
  lesson_title?: string | null;
  created_at: string;
};

export type BenchmarkRecord = {
  id: string;
  ticker?: string | null;
  name: string;
  description?: string | null;
  benchmark_type: string;
  created_at: string;
};

export type ShadowOpportunityProfile = {
  should_launch?: boolean;
  name?: string | null;
  family_key?: string | null;
  family_description?: string | null;
  opportunity_type?: string | null;
  priority_score?: number | null;
  signal_stage?: string | null;
  why_now?: string | null;
  priced_in_assessment?: string | null;
  investable_thesis?: string | null;
  portfolio_transmission?: string | null;
  expected_edge?: string | null;
  leading_indicators?: string[];
  lagging_confirmations?: string[];
  evidence_refs?: string[];
  evidence_to_check?: string[];
  falsification_tests?: string[];
  risk_controls?: string[];
  uncertainties?: string[];
  policy?: string | null;
  operator_prompt?: string | null;
  horizon?: string | null;
  no_launch_reason?: string | null;
  trigger_reason?: string | null;
  captured_at?: string | null;
  evidence_snapshot?: Array<{
    ref: string;
    kind?: string | null;
    ticker?: string | null;
    as_of?: string | null;
    source?: string | null;
    url?: string | null;
    summary?: string | Record<string, unknown> | null;
  }>;
};

export type AgentTurn = {
  session_id: string;
  assistant_message: string;
  subject_id: string;
  subject_type: string;
  subject_name?: string | null;
  resolution_reason?: string | null;
  process_mode?: string | null;
  reasoning_run_id?: string | null;
  stance?: string | null;
  confidence_band?: string | null;
  thesis_summary?: string | null;
  rationale_summary?: string | null;
  source_feedback_influence?: SourceFeedbackInfluence | null;
  historical_analogy_lenses?: HistoricalAnalogyLens[] | null;
  actions: Array<{
    action_type: string;
    status: string;
    summary: string;
    resource_id?: string | null;
    resource_type?: string | null;
  }>;
  subagents?: Record<string, string> | null;
  responded_at: string;
};

export type HistoricalAnalogyLens = {
  name?: string | null;
  period?: string | null;
  lens_use_policy?: string | null;
  current_application_prompt?: string | null;
  what_rhymes?: string | null;
  dominant_channel_test?: string | null;
  where_analogy_breaks?: string | null;
  portfolio_transmission?: string | null;
  best_next_check?: string | null;
  investor_questions?: string[] | null;
};

export type SourceFeedbackInfluence = {
  counts?: {
    useful?: number;
    not_useful?: number;
  };
  recent?: Array<Record<string, unknown>>;
  summary?: string | null;
};

export type AgentTurnJob = {
  job_id: string;
  status: string;
  request_message: string;
  session_id?: string | null;
  created_at: string;
  updated_at: string;
  events: Array<{
    phase: string;
    message: string;
    created_at: string;
    detail?: Record<string, unknown> | null;
    metadata?: Record<string, unknown> | null;
  }>;
  result?: AgentTurn | null;
  error?: string | null;
};

export type AgentTurnJobList = {
  jobs: AgentTurnJob[];
};

export type AgentResolve = {
  subject_id: string;
  subject_type: string;
  subject_name: string;
  resolution_reason: string;
  candidates: Array<{
    subject_id: string;
    subject_type: string;
    subject_name: string;
    score: number;
    reason: string;
  }>;
};

export type AgentConversationHistory = {
  session_id?: string | null;
  subject_id: string;
  subject_type: string;
  entries: Array<{
    id: string;
    role: string;
    content: string;
    created_at: string;
    message_kind?: string;
    is_artifact?: boolean;
    origin?: string | null;
    process_mode?: string | null;
    resolution_reason?: string | null;
    reasoning_run_id?: string | null;
    stance?: string | null;
    confidence_band?: string | null;
    thesis_summary?: string | null;
    rationale_summary?: string | null;
    source_feedback_influence?: SourceFeedbackInfluence | null;
    historical_analogy_lenses?: HistoricalAnalogyLens[] | null;
    actions: Array<{
      action_type: string;
      status: string;
      summary: string;
      resource_id?: string | null;
      resource_type?: string | null;
    }>;
    subagents?: Record<string, string> | null;
  }>;
};

export type AgentConversationSummary = {
  session_id: string;
  title: string;
  subject_id?: string | null;
  subject_type?: string | null;
  subject_name?: string | null;
  latest_message_preview?: string | null;
  artifact_count?: number;
  updated_at: string;
};

export type AgentConversationList = {
  conversations: AgentConversationSummary[];
};

export type ReasoningTrace = {
  id: string;
  run_type: string;
  model_used: string;
  model_version?: string | null;
  prompt_hash?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cost_usd?: number | null;
  duration_ms?: number | null;
  created_at: string;
  output_text?: string | null;
  structured_output_json: Record<string, unknown>;
  evidence_packet?: {
    id: string;
    query_text?: string | null;
    subject_type?: string | null;
    subject_id?: string | null;
    assembled_at: string;
    retrieval_layers_used: string[];
    gap_flags: string[];
    total_token_estimate?: number | null;
    direct_evidence_count: number;
    connected_evidence_count: number;
    historical_evidence_count: number;
    contradiction_evidence_count: number;
    coverage_snapshot: Record<string, unknown>;
    portfolio_context: Record<string, unknown>;
  } | null;
  critique?: {
    id: string;
    model_used: string;
    critique_text: string;
    issues_found: string[];
    severity: string;
    input_tokens?: number | null;
    output_tokens?: number | null;
    created_at: string;
  } | null;
};

export type RiskSummary = {
  as_of: string;
  active_benchmark?: BenchmarkRecord | null;
  benchmark_current_price?: number | null;
  portfolio_return_pct?: number | null;
  benchmark_return_pct?: number | null;
  active_return_pct?: number | null;
  measurement_start?: string | null;
  top_sector?: string | null;
  top_sector_weight_pct: number;
  top_holding?: string | null;
  top_holding_weight_pct: number;
  concentration_hhi: number;
  sector_exposures: Array<{
    label: string;
    weight_pct: number;
    detail?: string | null;
  }>;
  asset_class_exposures: Array<{
    label: string;
    weight_pct: number;
    detail?: string | null;
  }>;
  top_positions: Array<{
    label: string;
    weight_pct: number;
    detail?: string | null;
  }>;
  current_regime?: {
    regime_type: string;
    confidence: number;
    signal_source: string;
    start_date: string;
    end_date?: string | null;
  } | null;
  scenarios: Array<{
    name: string;
    scenario_description: string;
    total_portfolio_impact: number;
    portfolio_impact_json: Record<string, number>;
    computed_at: string;
  }>;
};

export type PerformanceAttribution = {
  as_of: string;
  period_start: string;
  window_days: number;
  method: string;
  total_beginning_value: number;
  total_ending_value: number;
  net_flow: number;
  gain: number;
  return_pct?: number | null;
  benchmark_ticker?: string | null;
  benchmark_return_pct?: number | null;
  active_return_pct?: number | null;
  covered_positions: number;
  total_positions: number;
  coverage_pct: number;
  unavailable_tickers: string[];
  items: Array<{
    ticker: string;
    name: string;
    sector: string;
    start_quantity: number;
    end_quantity: number;
    start_price: number;
    end_price: number;
    start_price_time: string;
    end_price_time: string;
    beginning_value: number;
    ending_value: number;
    net_flow: number;
    gain: number;
    contribution_pct: number;
    return_pct?: number | null;
    capital_return_pct?: number | null;
    transaction_count: number;
    data_status: string;
    status_detail?: string | null;
  }>;
};

export type DecisionReview = {
  id: string;
  decision_journal_id: string;
  outcome_assessment: string;
  actual_return?: number | null;
  mistake_preventable?: boolean | null;
  what_went_right?: string | null;
  what_went_wrong?: string | null;
  what_to_improve?: string | null;
  extracted_lessons: Array<{
    id: string;
    title: string;
    summary: string;
    lesson_type: string;
    created_at: string;
  }>;
  reviewed_at: string;
};

export type DecisionJournal = {
  id: string;
  position_id?: string | null;
  position_label?: string | null;
  decision_type: string;
  rationale: string;
  expected_catalyst_timeframe?: string | null;
  expected_return?: number | null;
  created_at: string;
  reviews: DecisionReview[];
};

export type ReviewQueueItem = {
  id: string;
  item_type: string;
  item_id: string;
  item_label: string;
  priority_score: number;
  status: string;
  trigger_reason: string;
  why_now_summary: string;
  next_action: string;
  signal_tags: string[];
  size_factor: number;
  evidence_change_factor: number;
  contradiction_pressure: number;
  thesis_drift: number;
  catalyst_proximity: number;
  coverage_weakness: number;
  reasoning_run_id?: string | null;
  created_at: string;
};

export type Lesson = {
  id: string;
  title: string;
  summary: string;
  lesson_type: string;
  applicable_sectors: string[];
  applicable_regimes: string[];
  originating_decision_review_id?: string | null;
  originating_experiment_result_id?: string | null;
  experiment_family_id?: string | null;
  maturity_status: string;
  confidence_score: number;
  supporting_observations: number;
  contradicting_observations: number;
  neutral_observations: number;
  last_validated_at?: string | null;
  stale_after?: string | null;
  metadata_json: Record<string, unknown>;
  usage_count: number;
  created_at: string;
};

export type VerificationResult = {
  id: string;
  subject_id: string;
  subject_type: string;
  trigger: string;
  prior_stance: string;
  verified_stance: string;
  confidence_band: string;
  conclusion_changed: boolean;
  contradiction_coverage_status: string;
  missing_classes_found: string[];
  change_reasoning: string;
  what_would_falsify: string[];
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  verified_at: string;
};

export async function apiFetch<T>(
  path: string,
  init?: RequestInit,
  timeoutMs: number = API_TIMEOUT_MS,
): Promise<T> {
  const url = `${API_BASE}${path}`;
  const fallbackBase = alternateLoopbackBase(API_BASE);
  const fallbackUrl = fallbackBase ? `${fallbackBase}${path}` : null;
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(new DOMException("Request timed out", "AbortError")),
    timeoutMs,
  );
  try {
    if (init?.signal) {
      const upstreamSignal = init.signal;
      if (init.signal.aborted) {
        controller.abort(upstreamSignal.reason);
      } else {
        upstreamSignal.addEventListener("abort", () => controller.abort(upstreamSignal.reason), { once: true });
      }
    }
    const requestInit: RequestInit = {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
      cache: "no-store",
      signal: controller.signal,
    };

    let response: Response;
    try {
      response = await fetch(url, requestInit);
    } catch (primaryErr) {
      if (!fallbackUrl) {
        throw primaryErr;
      }
      response = await fetch(fallbackUrl, requestInit);
    }

    if (!response.ok) {
      let errorDetail = "";
      try {
        const body = await response.json();
        errorDetail = body.detail || JSON.stringify(body);
      } catch {
        try {
          errorDetail = await response.text();
        } catch {
          errorDetail = response.statusText;
        }
      }
      throw new Error(errorDetail || `Request failed with status ${response.status}`);
    }

    if (response.status === 204) {
      return undefined as T;
    }
    return response.json() as Promise<T>;
  } catch (err) {
    if (err instanceof Error) {
      if (err.name === "AbortError") {
        throw new Error(`Request timeout: Prophet did not respond within ${Math.round(timeoutMs / 1000)}s for ${path}.`);
      }
      // Handle the generic "Failed to fetch" browser error for clarity
      if (err.message.toLocaleLowerCase().includes("failed to fetch")) {
        throw new Error(`Connectivity failure: unable to reach the Prophet backend at ${API_BASE}. Check that the local API is running.`);
      }
      throw err;
    }
    throw new Error("System transition error: An unexpected request failure occurred.");
  } finally {
    clearTimeout(timeout);
  }
}

export type ReconcileDiff = {
  ticker: string;
  kind: "missing_in_book" | "extra_in_book" | "quantity_mismatch";
  book_quantity: number;
  broker_quantity: number;
  delta: number;
};

export type CashDiscrepancy = {
  book_cash: number;
  broker_cash: number;
  delta: number;
};

export type ReconcileResponse = {
  in_sync: boolean;
  discrepancies: ReconcileDiff[];
  cash_discrepancy?: CashDiscrepancy | null;
  review_items_created: number;
};

export async function reconcileFromText(
  text: string,
  createReviewItems: boolean,
): Promise<ReconcileResponse> {
  return apiFetch<ReconcileResponse>("/portfolio/reconcile/text", {
    method: "POST",
    body: JSON.stringify({ text, create_review_items: createReviewItems }),
  });
}

export async function correctTransaction(
  transactionId: string,
  payload: TransactionCorrectionRequest,
): Promise<Transaction> {
  return apiFetch<Transaction>(`/portfolio/transactions/${transactionId}/correct`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
