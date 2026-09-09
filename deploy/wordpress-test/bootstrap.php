<?php
// Input/output containing credentials is captured by provision.py, never logged.
define('WP_INSTALLING', true);
require '/var/www/html/wp-load.php';
require_once ABSPATH . 'wp-admin/includes/upgrade.php';
$credentials = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
if (!is_blog_installed()) {
    wp_install('Article Agent Test Site', $credentials['admin_username'],
        'test-admin@example.invalid', false, '', $credentials['admin_password']);
    // Keep the automatically created sample out of the public article feed.
    wp_update_post(['ID' => 1, 'post_status' => 'draft']);
}
$user = get_user_by('login', $credentials['username']);
if (!$user) {
    $id = wp_insert_user([
        'user_login' => $credentials['username'],
        'user_pass' => $credentials['web_password'],
        'user_email' => 'test-editor@example.invalid',
        'role' => 'editor',
    ]);
    if (is_wp_error($id)) { WP_CLI::error('Could not create test editor.'); }
    $user = get_user_by('id', $id);
}
if (empty($credentials['application_password'])) {
    $result = WP_Application_Passwords::create_new_application_password(
        $user->ID, ['name' => 'Article Agent test upload']);
    if (is_wp_error($result)) { WP_CLI::error('Could not create application password.'); }
    $credentials['application_password'] = $result[0];
}
update_option('blog_public', 0);
update_option('blogdescription', 'Isolated WordPress draft upload test site');
update_option('permalink_structure', '/%postname%/');
echo json_encode($credentials, JSON_THROW_ON_ERROR);
