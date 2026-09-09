<?php
declare(strict_types=1);

add_action('after_setup_theme', static function (): void {
    add_theme_support('title-tag');
    add_theme_support('post-thumbnails');
});

add_action('wp_enqueue_scripts', static function (): void {
    wp_enqueue_style('article-agent-template', get_stylesheet_uri(), [], '0.1.0');
});
