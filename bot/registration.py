"""Пошаговая анкета регистрации участников квеста."""
from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from app.database import async_session_maker
from app.models import Player, RegistrationForm
from sqlalchemy import select

from bot.keyboards import main_kb

router = Router(name="registration")

CURRENT_EVENT_ID = 1

# --- Текст согласия ---
PRIVACY_CONSENT_TEXT = """🔒 *Согласие на обработку персональных данных*

Нажимая кнопку «Согласен», я подтверждаю, что даю согласие организаторам квеста «94 оттенка любви» на обработку моих персональных данных, предоставленных в анкете (ФИО, учебное заведение, курс, контактные данные Telegram, а также иные сведения, указанные мной добровольно).

Персональные данные используются исключительно для:
• регистрации и участия в квесте,
• связи со мной по вопросам проведения мероприятия,
• формирования команд и организации игрового процесса.

Данные не передаются третьим лицам и не используются в коммерческих целях.

Согласие действует до окончания квеста и подведения итогов.
Я понимаю, что могу отозвать согласие в любой момент, написав организаторам."""

# --- Кнопки ---
BTN_SKIP = "⏭ Пропустить"
BTN_OTHER = "Другое"

UNIVERSITIES = ["ИТМО", "СПбГУ", "Политех", BTN_OTHER]
COURSE_OPTIONS = ["1 курс", "2 курс", "3 курс", "4 курс", "5 курс", "6 курс", "Магистр", "Аспирант", "Выпускник", BTN_OTHER]
PARTICIPATION_FORMAT = ["Один", "Есть пара или команда"]


def skip_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SKIP)]],
        resize_keyboard=True,
    )


def universities_kb() -> ReplyKeyboardMarkup:
    row1 = [KeyboardButton(text=t) for t in UNIVERSITIES[:3]]
    row2 = [KeyboardButton(text=UNIVERSITIES[3])]
    return ReplyKeyboardMarkup(keyboard=[row1, row2], resize_keyboard=True)


def course_kb() -> ReplyKeyboardMarkup:
    rows = []
    for i in range(0, len(COURSE_OPTIONS), 3):
        rows.append([KeyboardButton(text=t) for t in COURSE_OPTIONS[i:i+3]])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def participation_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in PARTICIPATION_FORMAT]],
        resize_keyboard=True,
    )


def consent_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Согласен")],
            [KeyboardButton(text="Не согласен")],
        ],
        resize_keyboard=True,
    )


# --- FSM States ---
from aiogram.fsm.state import State, StatesGroup


class RegStates(StatesGroup):
    full_name = State()
    university = State()
    university_other = State()
    course = State()
    participation_format = State()
    partner_name = State()
    isu_number = State()
    interests = State()
    music = State()
    films_games = State()
    comment = State()
    photo = State()
    consent = State()


@router.message(StateFilter(RegStates), F.text.in_(["✍️ Регистрация", "Отмена", "/cancel"]))
async def cancel_or_restart_registration(message: Message, state: FSMContext):
    """При «Регистрация» или «Отмена» во время анкеты — начать заново."""
    await start_registration(message, state)


async def start_registration(message: Message, state: FSMContext):
    """Начать анкету."""
    await state.set_state(RegStates.full_name)
    await message.answer(
        "📋 *Анкета регистрации*\n\n"
        "Пройди анкету по шагам. Для необязательных вопросов можно нажать «Пропустить».\n\n"
        "_1/12_\n"
        "ФИО участника:",
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.full_name), F.text)
async def step_full_name(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 2:
        await message.answer("Введи ФИО (минимум 2 символа).")
        return
    await state.update_data(full_name=message.text.strip())
    await state.set_state(RegStates.university)
    await message.answer(
        "_2/12_\n"
        "Где учишься?",
        reply_markup=universities_kb(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.university), F.text)
async def step_university(message: Message, state: FSMContext):
    text = message.text.strip()
    if text not in UNIVERSITIES:
        await message.answer("Выбери вариант кнопкой.")
        return
    await state.update_data(university=text)
    if text == BTN_OTHER:
        await state.set_state(RegStates.university_other)
        await message.answer(
            "Введи название учебного заведения:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await _go_course(message, state)


async def _go_course(message: Message, state: FSMContext):
    await state.set_state(RegStates.course)
    await message.answer(
        "_3/12_\n"
        "Курс / статус:",
        reply_markup=course_kb(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.university_other), F.text)
async def step_university_other(message: Message, state: FSMContext):
    await state.update_data(university_other=message.text.strip())
    await _go_course(message, state)


@router.message(StateFilter(RegStates.course), F.text)
async def step_course(message: Message, state: FSMContext):
    text = message.text.strip()
    if text not in COURSE_OPTIONS:
        await message.answer("Выбери вариант кнопкой.")
        return
    await state.update_data(course_status=text)
    await state.set_state(RegStates.participation_format)
    await message.answer(
        "_4/12_\n"
        "Формат участия:",
        reply_markup=participation_kb(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.participation_format), F.text)
async def step_participation(message: Message, state: FSMContext):
    text = message.text.strip()
    if text not in PARTICIPATION_FORMAT:
        await message.answer("Выбери вариант кнопкой.")
        return
    await state.update_data(participation_format=text)
    if text == "Есть пара или команда":
        await state.set_state(RegStates.partner_name)
        await message.answer(
            "_5/12_\n"
            "ФИО или ник напарника:",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return
    await _go_isu_or_skip(message, state)


@router.message(StateFilter(RegStates.partner_name), F.text)
async def step_partner(message: Message, state: FSMContext):
    if not message.text or len(message.text.strip()) < 1:
        await message.answer("Введи ФИО или ник.")
        return
    await state.update_data(partner_name=message.text.strip())
    await _go_isu_or_skip(message, state)


async def _go_isu_or_skip(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("university") == "ИТМО":
        await state.set_state(RegStates.isu_number)
        await message.answer(
            "_6/12_\n"
            "ISU номер (необязательно):",
            reply_markup=skip_kb(),
            parse_mode="Markdown",
        )
    else:
        await state.update_data(isu_number=None)
        await _go_interests(message, state)


@router.message(StateFilter(RegStates.isu_number), F.text)
async def step_isu(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == BTN_SKIP:
        await state.update_data(isu_number=None)
    else:
        await state.update_data(isu_number=text)
    await _go_interests(message, state)


async def _go_interests(message: Message, state: FSMContext):
    await state.set_state(RegStates.interests)
    await message.answer(
        "_7/12_\n"
        "Интересы / хобби (необязательно):",
        reply_markup=skip_kb(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.interests), F.text)
async def step_interests(message: Message, state: FSMContext):
    if message.text and message.text.strip() != BTN_SKIP:
        await state.update_data(interests=message.text.strip())
    else:
        await state.update_data(interests=None)
    await state.set_state(RegStates.music)
    await message.answer(
        "_8/12_\n"
        "Музыкальные предпочтения (необязательно):",
        reply_markup=skip_kb(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.music), F.text)
async def step_music(message: Message, state: FSMContext):
    if message.text and message.text.strip() != BTN_SKIP:
        await state.update_data(music_preferences=message.text.strip())
    else:
        await state.update_data(music_preferences=None)
    await state.set_state(RegStates.films_games)
    await message.answer(
        "_9/12_\n"
        "Любимые фильмы / сериалы / игры (необязательно):",
        reply_markup=skip_kb(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.films_games), F.text)
async def step_films(message: Message, state: FSMContext):
    if message.text and message.text.strip() != BTN_SKIP:
        await state.update_data(films_games=message.text.strip())
    else:
        await state.update_data(films_games=None)
    await _go_comment(message, state)


async def _go_comment(message: Message, state: FSMContext):
    await state.set_state(RegStates.comment)
    await message.answer(
        "_10/12_\n"
        "Комментарий / пожелания (необязательно):",
        reply_markup=skip_kb(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.comment), F.text)
async def step_comment(message: Message, state: FSMContext):
    if message.text and message.text.strip() != BTN_SKIP:
        await state.update_data(comment=message.text.strip())
    else:
        await state.update_data(comment=None)
    await state.set_state(RegStates.photo)
    await message.answer(
        "_11/12_\n"
        "Фото участника (необязательно)\n\n"
        "Отправь фото или нажми «Пропустить»:",
        reply_markup=skip_kb(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.photo), F.photo)
async def step_photo_ok(message: Message, state: FSMContext):
    photo = message.photo[-1]
    await state.update_data(photo_file_id=photo.file_id)
    await _go_consent(message, state)


@router.message(StateFilter(RegStates.photo), F.text)
async def step_photo_skip(message: Message, state: FSMContext):
    if message.text and message.text.strip() == BTN_SKIP:
        await state.update_data(photo_file_id=None)
        await _go_consent(message, state)
    else:
        await message.answer("Отправь фото или нажми «Пропустить».")


async def _go_consent(message: Message, state: FSMContext):
    await state.set_state(RegStates.consent)
    await message.answer(
        "_12/12_\n\n" + PRIVACY_CONSENT_TEXT,
        reply_markup=consent_kb(),
        parse_mode="Markdown",
    )


@router.message(StateFilter(RegStates.consent), F.text)
async def step_consent(message: Message, state: FSMContext):
    text = message.text.strip() if message.text else ""
    if text == "Не согласен":
        await state.clear()
        await message.answer(
            "❌ Без согласия на обработку персональных данных участие в квесте невозможно.\n\n"
            "Если передумаешь — нажми «Регистрация» снова.",
            reply_markup=main_kb(),
        )
        return
    if text != "Согласен":
        await message.answer("Нажми «Согласен» или «Не согласен».")
        return

    data = await state.get_data()
    await state.clear()

    tg_id = message.from_user.id
    university = data.get("university", "")
    university_other = data.get("university_other") if university == BTN_OTHER else None

    async with async_session_maker() as db:
        # Upsert RegistrationForm
        r = await db.execute(
            select(RegistrationForm).where(
                RegistrationForm.event_id == CURRENT_EVENT_ID,
                RegistrationForm.tg_id == tg_id,
            )
        )
        existing = r.scalar_one_or_none()
        form_data = {
            "event_id": CURRENT_EVENT_ID,
            "tg_id": tg_id,
            "full_name": data.get("full_name", ""),
            "university": university,
            "university_other": university_other,
            "course_status": data.get("course_status", ""),
            "participation_format": data.get("participation_format", ""),
            "partner_name": data.get("partner_name"),
            "isu_number": data.get("isu_number"),
            "interests": data.get("interests"),
            "music_preferences": data.get("music_preferences"),
            "films_games": data.get("films_games"),
            "character_type": None,
            "comment": data.get("comment"),
            "photo_file_id": data.get("photo_file_id"),
            "privacy_consent": True,
        }
        if existing:
            for k, v in form_data.items():
                setattr(existing, k, v)
        else:
            form = RegistrationForm(**form_data)
            db.add(form)

        # Create Player if not exists
        r = await db.execute(
            select(Player).where(
                Player.event_id == CURRENT_EVENT_ID,
                Player.tg_id == tg_id,
            )
        )
        player = r.scalar_one_or_none()
        if not player:
            player = Player(event_id=CURRENT_EVENT_ID, tg_id=tg_id)
            db.add(player)

        await db.commit()

    await message.answer(
        "✅ Спасибо! Ты зарегистрирован(а).\n\n"
        "Важно: зарегистрируйся ещё в системе ITMO Events:\n"
        "https://itmo.events/events/117006\n\n"
        "Скоро появится информация о старте квеста.",
        reply_markup=main_kb(),
    )
