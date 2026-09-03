# Remote Control

Remote Control дає змогу підключатися до Windows-комп'ютера через веб-браузер. Клієнт на віддаленому комп'ютері передає зображення та звук і приймає команди клавіатури та миші через веб-інтерфейс.

## Встановлення

Потрібні Windows і Python 3.13 або сумісна версія.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements_server.txt
python -m pip install -r requirements_web_service.txt
```

Якщо PowerShell не дозволяє активувати середовище, виконайте:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Запуск

1. Запустіть веб-сервер:

   ```powershell
   python web_server.py
   ```

   Відкрийте `http://localhost:5000` у браузері. Для іншого порту задайте змінну `PORT`:

   ```powershell
   $env:PORT = "8080"
   python web_server.py
   ```

2. На комп'ютері, яким потрібно керувати, запустіть клієнт:

   ```powershell
   python server.py
   ```

3. У веб-інтерфейсі введіть ID сервера та пароль, якщо він був заданий, після чого натисніть підключення.

## Параметри клієнта

```text
--password PASSWORD    пароль для підключення
--relay-url URL        адреса relay-сервера
--server-id ID         унікальний ID сервера
--debug                розширене журналювання
```

Наприклад:

```powershell
python server.py --server-id office-pc --password "your-password"
```

За замовчуванням клієнт використовує relay-сервер, налаштований у `server.py`. Веб-клієнт і віддалений клієнт мають бути доступні через мережу, якщо підключення виконується не з цього самого комп'ютера.

## Логи

Робочі журнали записуються до каталогу `logs/`. Цей каталог і локальні файли налаштування ігноруються Git.

## Безпека

Використовуйте складний пароль і не відкривайте веб-інтерфейс у публічний доступ без додаткового захисту. Не передавайте пароль третім особам.
