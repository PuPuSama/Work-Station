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

export type PromptKind = "outline" | "article";

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
  raw_draft_word_count?: number;
  initial_article_word_count?: number;
  humanized_article_word_count?: number;
  linked_article_word_count?: number;
  final_article_word_count?: number;
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
  };
  integrations?: {
    tavily_ready: boolean;
  };
};

export type BatchOperation =
  | "titles"
  | "products"
  | "outline"
  | "article"
  | "rewrite_article"
  | "humanize"
  | "restore_links"
  | "prepare_images"
  | "export_docx"
  | "generate_tdk"
  | "package_delivery";

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
