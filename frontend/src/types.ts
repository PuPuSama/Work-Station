export type WorkflowStatus =
  | "new"
  | "titles_ready"
  | "title_selected"
  | "outline_ready"
  | "draft_ready"
  | "docx_exported";

export type Product = {
  name: string;
  url: string;
  image_path: string;
  description: string;
};

export type TaskRecord = {
  id: string;
  week_folder: string;
  customer: string;
  topic_index: number;
  topic: string;
  competitor_keyword: string;
  competitor_blog: string;
  status: WorkflowStatus;
  task_dir: string;
  title_candidates: string[];
  selected_title: string;
  outline: string;
  article: string;
  products: Product[];
  docx_path: string;
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
};

export type ApiMessage = {
  message: string;
  data?: unknown;
};
