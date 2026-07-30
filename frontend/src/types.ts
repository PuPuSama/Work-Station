export type WorkflowStatus =
  | "new"
  | "titles_ready"
  | "title_selected"
  | "outline_ready"
  | "outline_confirmed"
  | "draft_ready"
  | "initial_ai_checked"
  | "humanized_ready"
  | "final_ai_checked"
  | "links_verified"
  | "images_ready"
  | "docx_exported";

export type Product = {
  product_id?: string;
  name: string;
  url: string;
  canonical_url?: string;
  image_path: string;
  description: string;
  reference_summary?: string;
  reference_facts?: string[];
  specifications?: Record<string, string>;
  reference_path?: string;
  asset_manifest_path?: string;
  asset_count?: number;
  selected_asset_id?: string;
  selection_confidence?: number | null;
  selection_reason?: string;
  discovery_source?: string;
  detail_page_verified?: boolean;
  asset_status?: string;
  asset_error?: string;
};

export type AiCheckRecord = {
  confirmed: boolean;
  score: number | null;
  report: string;
  screenshot_path: string;
  article_hash: string;
  confirmed_at: string;
};

export type ArticleLink = {
  anchor: string;
  url: string;
  count?: number;
  heading?: string;
  context?: string;
};

export type CompressionRecord = {
  required: boolean;
  attempted_at: string;
  before_words: number;
  after_words: number;
  prompt_version?: string;
};

export type ContentVersion = {
  kind: string;
  content: string;
  word_count: number;
  content_hash: string;
  created_at: string;
  source_kind: string;
  source_hash?: string;
  prompt_version?: string;
};

export type ArticleImage = {
  id: string;
  source_path: string;
  prepared_path: string;
  role: "hero" | "product" | string;
  anchor_heading: string;
  anchor_text?: string;
  anchor_after?: string;
  filename: string;
  marker: string;
  product_name?: string;
  product_url?: string;
  status?: string;
  error?: string;
  anchor_candidates?: Array<{
    id: string;
    heading: string;
    anchor_heading: string;
    level: number;
    line_index: number;
  }>;
};

export type LinkValidation = {
  passed: boolean;
  source_count: number;
  preserved_count: number;
  missing_links: ArticleLink[];
  unexpected_links: ArticleLink[];
  visible_text_unchanged: boolean | null;
  article_hash: string;
  verified_at: string;
  error: string;
};

export type WorkflowError = {
  code: string;
  message: string;
  stage: string;
  recoverable: boolean;
  blocking?: boolean;
};

export type TdkMetadata = {
  title: string;
  description: string;
  keywords: string[];
  description_character_count: number;
  source_article_hash: string;
  generated_at: string;
  prompt_version: string;
};

export type PromptKind = "outline" | "article" | "review";

export type PromptLibraryItem = {
  id: string;
  customer: string;
  name: string;
  kind: PromptKind;
  content: string;
  version: number;
  use_count: number;
  active: boolean;
  created_at: string;
  updated_at: string;
};

export type PromptDefaults = {
  customer: string;
  default_outline_prompt_id: string;
  default_article_prompt_id: string;
  default_review_prompt_id: string;
};

export type ProjectPromptLibrary = {
  prompts: PromptLibraryItem[];
  defaults: PromptDefaults;
};

export type PromptSnapshot = {
  prompt_id: string;
  name: string;
  kind: PromptKind;
  content: string;
  version: number;
  source: "system" | "project_default" | "library";
  captured_at: string;
};

export type PromptPreview = {
  snapshot: PromptSnapshot;
  effective_prompt: string;
};

export type SeoReviewDimension = {
  key: string;
  name: string;
  score: number;
  target_score: number;
  main_issue: string;
  needs_revision: boolean;
};

export type SeoReviewRisk = {
  kind: "number" | "url" | "brand" | "product";
  label: string;
  before: string;
  after: string;
  message: string;
};

export type SeoReviewChange = {
  id: string;
  operation: "replace" | "insert_after" | "delete" | "structure";
  dimension_key: string;
  title: string;
  rationale: string;
  target_text: string;
  model_proposed_text: string;
  reviewed_text: string;
  source_start: number;
  source_end: number;
  hard_problem: boolean;
  applicable: boolean;
  validation_errors: string[];
  risks: SeoReviewRisk[];
  decision: "pending" | "accepted" | "rejected";
  decided_at: string;
  decided_by: string;
  risk_confirmed: boolean;
  risk_confirmed_at: string;
  updated_at: string;
};

export type SeoReviewRun = {
  id: string;
  source_article: string;
  source_article_hash: string;
  source_revision: number;
  score: number;
  dimensions: SeoReviewDimension[];
  publish_ready: boolean;
  publish_recommendation: string;
  report: string;
  changes: SeoReviewChange[];
  status: "open" | "applied" | "completed";
  finalized_at: string;
  finalized_by: string;
  applied_article_hash: string;
  applied_revision: number | null;
  revised_article?: string;
  revised_article_hash?: string;
  prompt_snapshot: PromptSnapshot;
  primary_keyword: string;
  long_tail_keywords: string[];
  created_at: string;
};

export type SeoReviewPreview = {
  review_id: string;
  article: string;
  article_hash: string;
  accepted_change_ids: string[];
  pending_count: number;
  rejected_count: number;
  invalid_count: number;
  structure_valid: boolean;
};

export type TaskRecord = {
  schema_version?: number;
  revision?: number;
  workflow_error?: WorkflowError | null;
  id: string;
  week_folder: string;
  customer: string;
  brand_name?: string;
  project_introduction?: string;
  project_notes?: string;
  topic_notes?: string;
  title_generation_instruction?: string;
  outline_custom_prompt?: string;
  article_custom_prompt?: string;
  use_outline_custom_prompt?: boolean;
  use_article_custom_prompt?: boolean;
  outline_prompt_selection?: string;
  article_prompt_selection?: string;
  seo_review_prompt_selection?: string;
  last_outline_prompt_snapshot?: PromptSnapshot | null;
  last_article_prompt_snapshot?: PromptSnapshot | null;
  include_project_introduction?: boolean;
  include_project_notes?: boolean;
  include_topic_notes?: boolean;
  source_key?: string;
  source_kind?: string;
  synced_from_task_id?: string;
  synced_from_week?: string;
  topic_index: number;
  topic: string;
  competitor_keyword: string;
  competitor_blog: string;
  status: WorkflowStatus;
  task_dir: string;
  title_candidates: string[];
  selected_title: string;
  outline: string;
  outline_draft?: string;
  article: string;
  raw_draft_article?: string;
  initial_article?: string;
  humanized_article?: string;
  humanization_skipped?: boolean;
  linked_article?: string;
  final_article?: string;
  article_versions?: ContentVersion[];
  seo_primary_keyword?: string;
  seo_long_tail_keywords?: string[];
  seo_reviews?: SeoReviewRun[];
  raw_draft_word_count?: number;
  raw_draft_hash?: string;
  initial_article_word_count?: number;
  initial_article_hash?: string;
  humanized_article_word_count?: number;
  humanized_article_hash?: string;
  linked_article_word_count?: number;
  linked_article_hash?: string;
  final_article_word_count?: number;
  final_article_hash?: string;
  compression?: CompressionRecord;
  initial_ai_check?: AiCheckRecord;
  final_ai_check?: AiCheckRecord;
  source_links?: ArticleLink[];
  link_validation?: LinkValidation;
  products: Product[];
  hero_image?: string;
  images?: ArticleImage[];
  transition_added?: boolean;
  allowed_actions?: string[];
  initial_article_ready?: boolean;
  initial_article_issues?: string[];
  legacy_export?: boolean;
  docx_path: string;
  tdk?: TdkMetadata;
  tdk_path?: string;
  delivery_package_path?: string;
  zero_gpt_report: string;
  created_at: string;
  updated_at: string;
};

export type DashboardSummary = {
  week_folder: string;
  week_path: string;
  customer_count: number;
  task_count: number;
  completed_count: number;
  status_counts: Record<string, number>;
  llm_ready: boolean;
};

export type PublicConfig = {
  topic_library: string;
  knowledge_base: string;
  output_root: string;
  data_file: string;
  current_week_folder: string;
  current_week_path: string;
  article: {
    language: string;
    title_candidates: number;
    default_word_count: number;
    ai_pass_threshold: number;
  };
  prompts?: {
    humanize: string;
  };
  docx_format: {
    font: string;
    title_1_size: number;
    title_2_size: number;
    title_3_size: number;
    body_size: number;
  };
  llm: {
    provider: string;
    base_url: string;
    model: string;
    reasoning_effort: string;
    available_models: string[];
    available_reasoning_efforts: string[];
  };
  integrations?: {
    tavily_ready: boolean;
  };
  features?: {
    knowledge_agent_enabled: boolean;
  };
};

export type KnowledgeSourceSummary = {
  project_id: string;
  source_id: string;
  display_name: string;
  source_kind: string;
  trust_tier: string;
  status: string;
  canonical_url: string | null;
  current_snapshot_id: string | null;
  snapshot_count: number;
  chunk_count: number;
  asset_count: number;
  latest_fetched_at: string | null;
  classification_reason: string;
  raw_evidence_url: string | null;
};

export type KnowledgeProductSummary = {
  project_id: string;
  product_id: string;
  name: string;
  status: string;
  canonical_url: string | null;
  category_path: string[];
};

export type KnowledgeLibrary = {
  project_id: string;
  source_count: number;
  inbox_count: number;
  published_count: number;
  product_count: number;
  confirmed_product_count: number;
  asset_count: number;
  sources: KnowledgeSourceSummary[];
  products: KnowledgeProductSummary[];
};

export type KnowledgeRetrievalScope = {
  project_id: string;
  retrieval_plan_id: string;
  scope_id: string;
  ordinal: number;
  scope_type: "introduction" | "h2_section" | "product_fact" | "faq";
  scope_key: string;
  title: string;
  query_variants: string[];
  filters: Record<string, unknown>;
  minimum_hits: number;
  minimum_distinct_sources: number;
  require_hard_fact: boolean;
  metadata: Record<string, unknown>;
};

export type KnowledgeRetrievalPlan = {
  project_id: string;
  retrieval_plan_id: string;
  article_id: string;
  outline_version: number;
  max_gap_fill_rounds: number;
  scopes: KnowledgeRetrievalScope[];
  metadata: Record<string, unknown>;
  created_at: string;
};

export type KnowledgeEvidenceHit = {
  chunk_id: string;
  text: string;
  heading_path: string[];
  score: number;
  provenance: {
    source_id: string;
    snapshot_id: string;
    display_name: string;
    source_kind: string;
    trust_tier: string;
    public_source: boolean;
    canonical_url: string | null;
    fetched_at: string | null;
  } | null;
  explanation: Record<string, unknown>;
};

export type KnowledgeEvidencePack = {
  project_id: string;
  evidence_pack_id: string;
  retrieval_plan_id: string;
  scope_id: string;
  article_id: string;
  outline_version: number;
  scope_type: string;
  scope_key: string;
  sufficiency: "sufficient" | "weak" | "missing";
  gap_reasons: string[];
  hard_fact_chunk_ids: string[];
  public_citation_urls: string[];
  hits: KnowledgeEvidenceHit[];
  created_at: string;
};

export type KnowledgeUploadResult = {
  project_id: string;
  source_id: string;
  snapshot_id: string;
  status: string;
  parser_name: string;
  parser_version: string;
  chunk_count: number;
  asset_count: number;
  message: string;
};

export type WordPressProbeResult = {
  project_id: string;
  site_url: string;
  detected: boolean;
  rest_api_url: string | null;
  namespaces: string[];
  route_count: number;
  reason: string;
  probe_version: string;
};

export type WordPressSyncedPage = {
  source_id: string;
  snapshot_id: string;
  page_type: string;
  canonical_url: string;
  status: string;
  product_id: string | null;
  asset_count: number;
  warnings: string[];
};

export type WordPressSyncResult = {
  project_id: string;
  wordpress_detected: boolean;
  category: WordPressSyncedPage;
  products: WordPressSyncedPage[];
  skipped_urls: string[];
  warnings: string[];
};

export type BatchOperation =
  | "titles"
  | "products"
  | "outline"
  | "article"
  | "rewrite_article"
  | "seo_review"
  | "humanize"
  | "restore_links"
  | "prepare_images"
  | "export_docx"
  | "generate_tdk"
  | "package_delivery"
  | "knowledge_research";

export type ResearchRunStatus =
  | "queued"
  | "running"
  | "waiting_for_review"
  | "completed"
  | "completed_with_warnings"
  | "failed"
  | "cancelled";

export type ResearchRun = {
  project_id: string;
  thread_id: string;
  organization_id: string;
  retrieval_plan_id: string;
  article_id: string;
  outline_version: number;
  status: ResearchRunStatus;
  current_node: string;
  current_scope_id: string | null;
  gap_fill_round: number;
  max_gap_fill_rounds: number;
  discovery_queries_used: number;
  max_discovery_queries: number;
  evidence_pack_ids: string[];
  warnings: string[];
  error_code: string | null;
  error_message: string | null;
  created_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
};

export type ResearchRunEvent = {
  sequence: number;
  event_type: string;
  node_name: string;
  scope_id: string | null;
  attempt: number;
  details: Record<string, unknown>;
  created_at: string | null;
};

export type ResearchGapFillAttempt = {
  scope_id: string;
  round_number: number;
  attempt_id: string;
  reason: string;
  channel: string;
  query: string;
  discovered_urls: string[];
  published_source_ids: string[];
  result: string;
  cost_usage: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
};

export type ResearchRunDetail = ResearchRun & {
  events: ResearchRunEvent[];
  gap_fill_attempts: ResearchGapFillAttempt[];
  review_candidates: Array<Record<string, unknown>>;
};

export type ResearchRunQueued = {
  run: ResearchRun;
  queue_batch_id: string;
  queue_job_id: string;
};

export type BatchJobStatus =
  | "queued"
  | "running"
  | "retry_wait"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "conflict";

export type BatchJobRecord = {
  id: string;
  batch_id: string;
  task_id: string;
  customer: string;
  topic_index: number;
  topic: string;
  operation: BatchOperation;
  status: BatchJobStatus;
  request: Record<string, unknown>;
  source_revision: number;
  result_revision: number | null;
  attempts: number;
  max_attempts: number;
  available_at: number;
  cancel_requested: boolean;
  error: string;
  created_at: string;
  started_at: string;
  finished_at: string;
  updated_at: string;
};

export type BatchRecord = {
  id: string;
  operation: BatchOperation;
  customer: string;
  status:
    | "queued"
    | "running"
    | "succeeded"
    | "cancelled"
    | "completed_with_errors";
  total: number;
  completed: number;
  status_counts: Record<string, number>;
  jobs: BatchJobRecord[];
  created_at: string;
  updated_at: string;
};

export type BatchCreateResponse = {
  batch: BatchRecord | null;
  rejected: Array<{ task_id: string; message: string }>;
};

export type ApiMessage = {
  message: string;
  data?: unknown;
};
