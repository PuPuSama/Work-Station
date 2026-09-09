<?php get_header(); while (have_posts()): the_post(); ?>
<article class="article-card"><h1 class="entry-title"><?php the_title(); ?></h1>
<div class="entry-content"><?php the_content(); ?></div></article>
<?php endwhile; get_footer(); ?>
