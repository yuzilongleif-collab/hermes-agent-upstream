// Notification settings copy used when the desktop locale loads.
export const ruNotifications = {
  minimumDurationTitle: 'Keep ordinary toasts for at least 10 seconds',
  minimumDurationDesc:
    'For new information and success toasts from Hermes and plugins in this client. Extends explicit short timings from enqueue, not visible time. Longer, sticky, actionable, warning and error timings stay unchanged; manual close remains available. OS alerts are separate.',
  title: 'Уведомления',
  intro: 'Системные уведомления (не всплывающие внутри приложения). Настраивается для каждого устройства.',
  enableAll: 'Включить уведомления',
  enableAllDesc: 'Если выключено — все уведомления ниже будут заглушены.',
  focusedHint: 'Оповещения о завершении срабатывают только пока Hermes в фоне.',
  kinds: {
    approval: {
      label: 'Нужно одобрение',
      description: 'Команда ожидает вашего одобрения или отклонения.'
    },
    input: {
      label: 'Нужен ввод',
      description: 'Hermes задал вопрос или требует пароль/секрет.'
    },
    turnDone: {
      label: 'Ответ готов',
      description: 'Ход завершился, пока Hermes был в фоне.'
    },
    turnError: {
      label: 'Ход не удался',
      description: 'Ошибки фоновых ходов.'
    },
    backgroundDone: {
      label: 'Фоновая задача завершена',
      description: 'Фоновая команда терминала выполнена.'
    },
    credits: {
      label: 'Оповещения о кредитах',
      description: 'Доступ к кредитам приостановлен или восстановлен.'
    },
    plugin: {
      label: 'Уведомления плагинов',
      description: 'Плагин приложения отправил уведомление, пока Hermes был в фоне.'
    }
  },
  test: 'Отправить тестовое уведомление',
  testTitle: 'Hermes',
  testBody: 'Уведомления работают.',
  testSent:
    'Тест отправлен. Если ничего не появилось, проверьте разрешения на уведомления в системе и режим «Не беспокоить».',
  testUnsupported: 'Эта система не поддерживает системные уведомления.',
  muteSoundsTitle: 'Отключить звуки интерфейса',
  muteSoundsDesc:
    'Отключает автоматические звуки завершения, активации и размышления, виброотклик и звуки системных уведомлений. Прослушивание звуков и голосовые ответы остаются включены.',
  completionSoundTitle: 'Звук завершения',
  completionSoundDesc: 'Воспроизводится, когда ход агента завершён. Выберите пресет и прослушайте здесь.',
  completionSoundPreview: 'Прослушать'
}
