# (c) 2017 Julian Gonggrijp

import sys
import os
import os.path as op
import logging
import mimetypes
from datetime import date, datetime
from itertools import zip_longest

from flask_script import Manager

from .utilities import maybe
from .parsers import document, latex_writer_sources, filename_parts
import campbellsoup.models as m

UNKNOWN_AUTHOR_NAME = 'unattributed'
UNKNOWN_AUTHOR_FULL_NAME = 'Unidentified Author'
REVISION_FMT = 'Auto import of group {} within test "{}".'
ATTRIBUTION_FMT = '\nOther authors mentioned in archive: {}.'
IMPORTED_STATUS_NAME = 'imported'

logger = logging.getLogger(__name__)
_person_cache = {}
_format_cache = {}
_question_kind_cache = {}
_figure_kind_cache = {}
_status_cache = {}


def process_options(app, **kwargs):
    """ Obligatory, possibly useful in the future (see Flask-Script docs). """
    pass


# Attach to a parent Manager in order to use
import_manager = Manager(process_options)


@import_manager.command
def import_test(directory, title=None, year=None, order_by_stdin=False):
    """
    Import question group and image files from `directory` nonrecursively.
    
    Creates a new Test object to which the imported question groups will
    be associated. `title` is used as the name for the new test and `year`
    is used for its date.
    If not provided, `title` and `year` will be derived from the last
    path segment of `directory`.
    
    All files from `directory` are imported in numerical-alphabetical
    order, unless `order_by_stdin` is set. In that case, pass the names of
    the files to be imported on the standard input, one file per line. The
    names should include the file extension but exclude the leading path
    component, which is already provided through `directory`. If the
    manual listing includes figures, group the figures together with the
    text files they belong to, with the text file first, and list the
    figure files in the same order in which they appear in the text file.
    """
    if year is None:
        year = op.split(op.normpath(directory))[1]
    if title is None:
        title = year
    test_date = date(int(year), 6, 1)
    if order_by_stdin:
        files = sys.stdin.read().strip().split('\n')
    else:
        files = sorted(os.listdir(directory), key=filename_order_key)
    os.chdir(directory)
    logger.debug('Working directory: {}'.format(os.getcwd()))
    logger.debug('File list:\n{}'.format('\n'.join(files)))
    session = m.db.session
    try:
        test = m.Test(title=title, date=test_date)
        import_groups(files, test, session)
        session.add(test)
        session.commit()
    except:
        session.rollback()
        raise


def import_groups(files, test, session):
    """ Import `files` in the given order and add to `test`. """
    order = 1     # archives start at 1, so we do the same here
    blocks = []   # blocks that mention figures
    figures = []  # figures that will fill the gaps in `blocks`
    for filename in files:
        if op.splitext(filename)[1] == '.txt':
            # We first import a group and then any figures belonging to it.
            # So we bind the groups and figures together before starting a new
            # group.
            bind_figures(blocks, figures, session)
            blocks, figures = [], []
            group, blocks = import_textfile(
                filename,
                order,
                test.title,
                session,
            )
            session.add(m.TestGroupBinding(test=test, group=group, order=order))
            order += 1
        else:
            figures.append(import_figure(filename, group.revision, session))
    bind_figures(blocks, figures, session)


def import_textfile(filename, group_order, test_title, session):
    """ Parse and import the contents of an archive textfile. """
    logger.info('Importing textfile {}'.format(filename))
    try:
        text = open(filename).read()
    except ValueError:  # Silly Windows file
        text = open(filename, encoding='cp1252').read()
    tree = document.parseString(text, True)
    revision = make_revision(
        tree.get('authors'),
        group_order,
        test_title,
        session,
    )
    if 'contentPlain' in tree:
        return import_plain(tree, revision, session)
    elif 'contentLW' in tree:
        raw_blocks = latex_writer_sources.parseString(text, True)
        return import_latex_writer(tree, raw_blocks, revision, session)
    else:
        raise KeyError('No known content type in parse tree')


def make_revision(authors, group_order, test_title, session):
    """ Create a revision for the question group under consideration. """
    logger.debug('make_revision({}, {}, {})'.format(
        authors,
        group_order,
        test_title,
    ))
    now = datetime.now()
    message_first_line = REVISION_FMT.format(group_order, test_title)
    message_tail = ''
    if maybe(authors, 0) is None:
        logger.debug('make_revision: setting fallback author {}'.format(
            UNKNOWN_AUTHOR_NAME,
        ))
        author_objs = [get_person(
            UNKNOWN_AUTHOR_NAME,
            session,
            full_name=UNKNOWN_AUTHOR_FULL_NAME,
        )]
    else:
        author_objs = [
            get_person(name, session) for name in authors if name is not None
        ]
    assert len(author_objs) > 0
    if len(author_objs) > 1:
        message_tail = ATTRIBUTION_FMT.format(', '.join(authors[1:]))
    revision = m.Revision(
        author=author_objs[0],
        date=now,
        commit_msg=message_first_line+message_tail,
    )
    session.add(revision)
    return revision


def get_person(short_name, session, **kwargs):
    """ Try to fetch a Person from database, create if she does not exist. """
    global _person_cache
    short_name = short_name.strip()
    person = _person_cache.get(short_name)
    if person is None:
        kwargs.setdefault('full_name', short_name)
        person = session.query(m.Person).filter_by(
            short_name=short_name,
        ).one_or_none() or m.Person(
            short_name=short_name,
            **kwargs
        )
        _person_cache[short_name] = person
        session.add(person)
    return person


def make_group(revision, reuse, format_name, session):
    """ Common group creation logic in import_plain and import_latex_writer. """
    if maybe(reuse, 0) is not None and len(reuse) == 2:
        parent_title = str(reuse[0])  # actually the year, but works for now
        parent_order = reuse[1]
        parent = session.query(m.Group).join('test_bindings', 'test').filter(
            m.TestGroupBinding.order == parent_order,
            m.Test.title == parent_title,
        ).one()
        network = parent.network
    else:
        parent = None
        network = m.GroupNetwork()
    group = m.Group(
        revision=revision,
        format=get_category(format_name, _format_cache, session, m.Format),
        network=network,
    )
    if parent is not None:
        session.add(m.GroupHistory(parent=parent, child=group))
    session.add(group)
    return group


def get_category(name, cache, session, model):
    """ Return existing `model` instance if available, create if necessary. """
    instance = cache.get(name)
    if instance is None:
        instance = session.query(model).filter_by(
            name=name,
        ).one_or_none() or model(name=name)
        cache[name] = instance
        session.add(instance)
    return instance


def import_plain(tree, revision, session):
    """ Import a group and its blocks from parsing `tree` in plain notation. """
    group = make_group(revision, tree.get('reuse'), 'text/plain', session)
    plain_blocks = tree['contentPlain']
    block_count = len(plain_blocks)
    question_count = maybe(tree, 'questionCount', 0) or block_count
    intro_count = 0
    if block_count < question_count:
        logger.error('{} questions claimed but only {} blocks found'.format(
            question_count,
            block_count,
        ))
    elif block_count > question_count:
        intro_count = block_count - question_count
    group.title = maybe(tree, 'title', 0)
    if not group.title and intro_count > 0:
        group.title = plain_blocks[0].splitlines()[0]
    intros, questions, question_bindings = import_plain_blocks(
        plain_blocks,
        intro_count,
        group,
        session,
    )
    questions[0].answer = maybe(tree, 'answer', 0)
    if 'points' in tree:
        plain_attach_global_points(tree['points'], question_bindings)
    images = tree.get('images')
    if maybe(images, 0) is not None:
        all_blocks = intros + questions
        # Attach all yet-to-be-imported images to the first block
        return group, all_blocks[0:1] * len(images)
    return group, []


def import_plain_blocks(plain_blocks, intro_count, group, session):
    """ Import Introductions and Questions in `group` in plain notation. """
    intros = [m.Introduction(
        revision=group.revision,
        text=text,
    ) for text in plain_blocks[:intro_count]]
    session.add_all(m.GroupIntroductionBinding(
        group=group,
        introduction=intro,
        order=index,
    ) for index, intro in enumerate(intros, start=1))
    questions = [m.Question(
        revision=group.revision,
        status=get_import_status(session),
        network=m.QuestionNetwork(),
        text=text,
    ) for text in plain_blocks[intro_count:]]
    question_bindings = [m.GroupQuestionBinding(
        group=group,
        question=question,
        order=index,
    ) for index, question in enumerate(questions, start=intro_count+1)]
    session.add_all(question_bindings)
    return intros, questions, question_bindings


def plain_attach_global_points(points, question_bindings):
    """ Attach globally declared max grades to plaintext question bindings. """
    if len(points) == 2:
        if points[0] != sum(points[1]):
            logger.warning('{} != {} points'.format(
                ' + '.join(points[1]),
                points[0],
            ))
        if len(points[1]) != len(question_bindings):
            logger.error('Mismatch: {} points, {} question_bindings'.format(
                len(points[1]),
                len(question_bindings),
            ))
        for question_binding, points in zip(question_bindings, points[1]):
            question_binding.weight = points
    else:
        question_bindings[0].weight = points[0]


def import_latex_writer(tree, sources, revision, session):
    """ Import a group and its blocks from `tree` with `sources` in LaTeX-w. """
    subtrees = tree['contentLW']
    assert len(subtrees) == len(sources)
    group = make_group(
        revision,
        tree.get('reuse'),
        'text/x-latex-writer',
        session,
    )
    group.title = maybe(subtrees, 0, 'title')
    figure_blocks = []
    for index, (subtree, source) in enumerate(zip(subtrees, sources), start=1):
        if 'question' in subtree:
            block = import_latex_writer_question(subtree, revision, session)
            session.add(m.GroupQuestionBinding(
                group=group,
                question=block,
                order=index,
                weight=maybe(subtree, 'points', 0),
            ))
        elif 'intro' in subtree or 'figure' in subtree:
            block = import_latex_writer_introduction(subtree, revision, session)
            session.add(m.GroupIntroductionBinding(
                group=group,
                introduction=block,
                order=index,
            ))
        else:
            # This should only happen with the first block, when it contains
            # the title and nothing else.
            assert index == 1
            assert len(source.splitlines()) == 1
            continue
        block.source_code = source
        block.figure_filenames = [
            subtree[fig][0]
                for fig in ('figure', 'answerfigure')
                    if fig in subtree
        ]
        figure_blocks.extend([block] * len(block.figure_filenames))
    return group, figure_blocks


def import_latex_writer_question(tree, revision, session):
    """ Import a single LW question and return as m.Question. """
    question = m.Question(
        revision=revision,
        status=get_import_status(session),
        network=m.QuestionNetwork(),
    )
    get_category_args = (_question_kind_cache, session, m.QuestionKind)
    if 'complete_text' in tree:
        question.kind = get_category('complete_text', *get_category_args)
        question.text = ' '.join(
            '({})'.format('/'.join(line)) if index % 2 else line
                for index, line in enumerate(tree['question'])
        )
    elif 'answerfigure' in tree:
        question.kind = get_category('answerfigure', *get_category_args)
    elif 'drawbox' in tree:
        question.kind = get_category('drawbox', *get_category_args)
    elif 'type' in tree:
        question.kind = get_category(tree['type'][0], *get_category_args)
    else:
        assert 'answerblock' in tree
        question.kind = get_category('mc', *get_category_args)
    if not question.text:
        question.text = tree['question']
    if 'table' in tree:
        question.text += '\n\n' + '\n'.join('; '.join(row)
            for row in tree['table']
        )
    question.answer = maybe(tree, 'answer', 0)
    if 'comments' in tree:
        question.notes = '\n'.join(': '.join(comment)
            for comment in tree['comments']
        )
    session.add(question)
    return question


def import_latex_writer_introduction(tree, revision, session):
    """ Import a single LW introduction and return as m.Introduction. """
    introduction = m.Introduction(
        revision=revision,
        text=tree.get('introduction'),
    )
    session.add(introduction)
    return introduction


def get_import_status(session):
    """ Return the single m.QuestionStatus object for imported questions. """
    return get_category(
        IMPORTED_STATUS_NAME,
        _status_cache,
        session,
        m.QuestionStatus,
    )


def import_figure(filename, revision, session):
    """ Import an image file as an m.Figure. """
    with open(filename, 'rb') as image_file:
        figure = m.Figure(
            revision=revision,
            # For the time being, we won't be terribly intelligent about
            # different kinds of figures. By default we assume it's "just a
            # figure". In the case of LaTeX-writer questions, the figure may be
            # marked explicitly as an answerfigure; in that case the kind is
            # reassigned later.
            kind=get_category(
                'figure',
                _figure_kind_cache,
                session,
                m.FigureKind,
            ),
            tree=m.FigureTree(),
            filename=filename,
            mimetype=mimetypes.guess_type(filename),
            contents=image_file.read(),
        )
    session.add(figure)
    return figure


def bind_figures(blocks, figures, session):
    """ Binds imported figures to blocks that need figures, adds to session. """
    for block, figure in zip_longest(blocks, figures):
        if block is None:
            logger.warning('Dangling figure: {}'.format(figure.filename))
            continue
        block_order = (
            block.groups[0].test_bindings[0].order,
            block.group_bindings[0].order,
        )
        if figure is None:
            logger.warning(
                'Block has unfulfilled figures: {}/{}'.format(*block_order),
            )
            continue
        logger.debug(
            'Binding block {}/{} with {}'.format(*block_order, figure.filename),
        )
        # Keep track of the no. of figures that are associated with each block.
        block.figure_counter = getattr(block, 'figure_counter', -1) + 1
        fname_len = 1  # sensible default for the last branch, cf __fname_comp
        if hasattr(block, 'figure_filenames'):
            # Special case for LaTeX-writer sources where Remie or Leonie has
            # renamed the image file, but the original file name is still
            # referenced in the source code. We prefer the original file name,
            # because it tends to be more informative.
            fname_len = len(block.figure_filenames)
            assert block.figure_counter < fname_len, '{} < {}'.format(
                block.figure_counter,
                fname_len,
            )
            figure.filename = block.figure_filenames[block.figure_counter]
        if isinstance(block, m.Introduction):
            session.add(m.IntroductionFigureBinding(
                introduction=block,
                figure=figure,
            ))
        else:
            assert isinstance(block, m.Question)
            if (
                block.kind is not None and
                block.kind.name == 'answerfigure' and
                block.figure_counter == fname_len - 1  # cf __fname_comp
                # This is a heuristic, because insufficient information is
                # available to know for sure which figure is the answerfigure.
            ):
                figure.kind = get_category(
                    'answerfigure',
                    _figure_kind_cache,
                    session,
                    m.FigureKind,
                )
            session.add(m.QuestionFigureBinding(question=block, figure=figure))


def filename_order_key(name):
    """
        Returns a tuple reflecting the proper order of `name`.
    
        Use as sorting key function.
    """
    root, extension = op.splitext(name)
    parts = filename_parts.parseString(root, True).asList()
    parts.insert(1, 'c' if extension == '.txt' else 'f')
    return parts
