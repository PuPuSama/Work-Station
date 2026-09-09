<?php
declare(strict_types=1);

// Source identity and TDK storage for the isolated publishing test site.
add_action('init', static function (): void {
    foreach (['article_agent_source_hash', 'article_agent_task_id',
              'article_agent_seo_title', 'article_agent_seo_description',
              'article_agent_seo_keywords'] as $key) {
        register_post_meta('post', $key, [
            'type' => 'string', 'single' => true, 'show_in_rest' => true,
            'auth_callback' => static function (): bool {
                return current_user_can('edit_posts');
            },
        ]);
    }
});
