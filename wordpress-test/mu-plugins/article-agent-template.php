<?php
declare(strict_types=1);

/* Test-only fields used to verify Article Agent's source identity and TDK mapping. */
add_action('init', static function (): void {
    foreach ([
        'article_agent_source_hash' => ['type' => 'string', 'show_in_rest' => true, 'single' => true],
        'article_agent_task_id' => ['type' => 'string', 'show_in_rest' => true, 'single' => true],
        'article_agent_seo_title' => ['type' => 'string', 'show_in_rest' => true, 'single' => true],
        'article_agent_seo_description' => ['type' => 'string', 'show_in_rest' => true, 'single' => true],
        'article_agent_seo_keywords' => ['type' => 'string', 'show_in_rest' => true, 'single' => true],
    ] as $key => $args) {
        register_post_meta('post', $key, array_merge($args, ['auth_callback' => static function (): bool { return current_user_can('edit_posts'); }]));
    }
});
