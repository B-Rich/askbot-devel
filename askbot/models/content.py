import datetime
import logging
from django.contrib.auth.models import User
from django.contrib.contenttypes import generic
from django.contrib.sitemaps import ping_google
from django.db import models
from askbot.models.meta import Comment, Vote, FlaggedItem
from askbot.models.user import EmailFeedSetting

class Content(models.Model):
    """
        Base class for Question and Answer
    """
    author = models.ForeignKey(User, related_name='%(class)ss')
    added_at = models.DateTimeField(default=datetime.datetime.now)

    wiki = models.BooleanField(default=False)
    wikified_at = models.DateTimeField(null=True, blank=True)

    locked = models.BooleanField(default=False)
    locked_by = models.ForeignKey(User, null=True, blank=True, related_name='locked_%(class)ss')
    locked_at = models.DateTimeField(null=True, blank=True)

    score = models.IntegerField(default=0)
    vote_up_count = models.IntegerField(default=0)
    vote_down_count = models.IntegerField(default=0)

    comment_count = models.PositiveIntegerField(default=0)
    offensive_flag_count = models.SmallIntegerField(default=0)

    last_edited_at = models.DateTimeField(null=True, blank=True)
    last_edited_by = models.ForeignKey(User, null=True, blank=True, related_name='last_edited_%(class)ss')

    html = models.TextField(null=True)#html rendition of the latest revision
    text = models.TextField(null=True)#denormalized copy of latest revision
    comments = generic.GenericRelation(Comment)
    votes = generic.GenericRelation(Vote)
    flagged_items = generic.GenericRelation(FlaggedItem)

    _use_markdown = True
    _urlize = False

    class Meta:
        abstract = True
        app_label = 'askbot'

    def get_comments(self):
        comments = self.comments.all().order_by('id')
        return comments

    #todo: maybe remove this wnen post models are unified
    def get_text(self):
        return self.text

    def add_comment(self, comment=None, user=None, added_at=None):
        if added_at is None:
            added_at = datetime.datetime.now()
        if None in (comment ,user):
            raise Exception('arguments comment and user are required')

        #Comment = models.get_model('askbot','Comment')#todo: forum hardcoded
        comment = Comment(
                            content_object=self, 
                            comment=comment, 
                            user=user, 
                            added_at=added_at
                        )
        comment.parse_and_save(author = user)
        self.comment_count = self.comment_count + 1
        self.save()
        return comment

    def get_instant_notification_subscribers(
                                self,
                                potential_subscribers = None,
                                mentioned_users = None,
                                exclude_list = None,
                            ):
        """get list of users who have subscribed to
        receive instant notifications for a given post
        this method works for questions and answers

        parameter "potential_subscribers" is not used here,
        but left for the uniformity of the interface (Comment method does use it)

        comment class has it's own variant which does have quite a bit
        of duplicated code at the moment
        """
        subscriber_set = set()

        #1) mention subscribers - common to questions and answers
        if mentioned_users:
            mention_subscribers = EmailFeedSetting.objects.filter(
                                            subscriber__in = mentioned_users,
                                            feed_type = 'm_and_c',
                                            frequency = 'i'
                                        ).values_list(
                                                'subscriber', 
                                                flat=True
                                        )
            subscriber_set.update(mention_subscribers)

        origin_post = self.get_origin_post()#handy to make generic method

        #2) individually selected - make sure that users
        #are individual subscribers to this question
        selective_subscribers = origin_post.followed_by.all()
        if selective_subscribers:
            selective_subscribers = EmailFeedSetting.objects.filter(
                                                subscriber__in = selective_subscribers,
                                                feed_type = 'q_sel',
                                                frequency = 'i'
                                            ).values_list(
                                                    'subscriber', 
                                                    flat=True
                                            )
            for subscriber in selective_subscribers:
                if origin_post.passes_tag_filter_for_user(subscriber):
                    subscriber_set.add(subscriber)

            subscriber_set.update(selective_subscribers)

        #3) whole askbot subscibers
        global_subscribers = EmailFeedSetting.objects.filter(
                                            feed_type = 'q_all',
                                            frequency = 'i'
                                        ).values_list(
                                                'subscriber', 
                                                flat=True
                                        )
        #todo: apply tag filters here
        subscriber_set.update(global_subscribers)

        #4) question asked by me
        question_author = origin_post.author
        if EmailFeedSetting.objects.filter(
                                            subscriber = question_author,
                                            frequency = 'i',
                                            feed_type = 'q_ask'
                                        ):
            subscriber_set.add(question_author)

        #4) questions answered by me -make sure is that people 
        #are authors of the answers to this question
        #todo: replace this with a query set method
        answer_authors = set()
        for answer in origin_post.answers.all():
            authors = answer.get_author_list()
            answer_authors.update(authors)

        if answer_authors:
            answer_authors = EmailFeedSetting.objects.filter(
                                            subscriber__in = answer_authors,
                                            frequency = 'i',
                                            feed_type = 'q_ans',
                                        ).values_list(
                                            'subscriber',
                                            flat=True
                                        )
            subscriber_set.update(answer_authors)
        subscriber_set -= set(exclude_list)

        return list(subscriber_set)

    def passes_tag_filter_for_user(user):

        post_tags = self.get_origin_post().tags.all()

        if user.tag_filter_setting == 'ignored':
            ignored_tags = user.tag_selections.filter(reason = 'bad')
            if set(post_tags) & set(ignored_tags):
                return False
            else:
                return True
        else:
            interesting_tags = user.tag_selections.filter(reason = 'good')
            if set(post_tags) & set(interesting_tags):
                return True
            else:
                return False

    def get_latest_revision(self):
        return self.revisions.all().order_by('-revised_at')[0]

    def get_latest_revision_number(self):
        return self.get_latest_revision().revision

    def get_time_of_last_edit(self):
        if self.last_edited_at:
            return self.last_edited_at
        else:
            return self.added_at

    def get_author_list(
                    self,
                    include_comments = False, 
                    recursive = False, 
                    exclude_list = None):

        #todo: there may be a better way to do these queries
        authors = set()
        authors.update([r.author for r in self.revisions.all()])
        if include_comments:
            authors.update([c.user for c in self.comments.all()])
        if recursive:
            if hasattr(self, 'answers'):
                for a in self.answers.exclude(deleted = True):
                    authors.update(a.get_author_list( include_comments = include_comments ) )
        if exclude_list:
            authors -= set(exclude_list)
        return list(authors)

    def passes_tag_filter_for_user(self, user):
        tags = self.get_origin_post().tags.all()

        if self.tag_filter_setting == 'interesting':
            #at least some of the tags must be marked interesting
            return self.tag_selections.exists(tag__in = tags, reason = 'good')

        elif self.tag_filter_setting == 'ignored':
            #at least one tag must be ignored
            if self.tag_selections.exists(tag__in = tags, reason = 'bad'):
                return False
            else:
                return True

        else:
            raise ValueError(
                        'unexpected User.tag_filter_setting %s' \
                        % self.tag_filter_setting
                    )

    def post_get_last_update_info(self):#todo: rename this subroutine
            when = self.added_at
            who = self.author
            if self.last_edited_at and self.last_edited_at > when:
                when = self.last_edited_at
                who = self.last_edited_by
            comments = self.comments.all()
            if len(comments) > 0:
                for c in comments:
                    if c.added_at > when:
                        when = c.added_at
                        who = c.user
            return when, who