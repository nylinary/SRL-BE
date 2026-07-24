# Принципы написания кода

<details>

<summary>Что такое SOLID?</summary>

Пять принципов ООП-дизайна.

1. **S**ingle responsibility
2. **O**pen/closed

</details>

<details>

<summary>Что такое KISS?</summary>

</details>

# БД

## PostgreSQL

<details>

<summary>Что такое <mark style="color:$primary;">MVCC</mark>?</summary>

Multiversion concurrency control — каждая транзакция видит свой снимок данных.

{% hint style="warning" %}
Раздувание таблиц лечится `VACUUM`.
{% endhint %}

{% code title="check.sql" %}
```sql
SELECT xmin, xmax FROM accounts;
```
{% endcode %}

<figure><img src="../.gitbook/assets/mvcc.png" alt=""><figcaption>Схема MVCC</figcaption></figure>

</details>

<details>

<summary>Уровни изоляции</summary>

<details>

<summary>Read Committed</summary>

Уровень по умолчанию в PostgreSQL.

</details>

<details>

<summary>Serializable</summary>

Самый строгий уровень.

</details>

</details>

## Redis

<details>

<summary>Зачем нужен Redis?</summary>

Кеш и брокер. Пример кода, который *не* должен ломать парсер:

```python
# </details> внутри кода
print("<details>")
```

Готово.

</details>

# Брокеры сообщений

## Apache Kafka

<details>

<summary>Архитектура в Kafka</summary>

{% tabs %}
{% tab title="Брокер" %}
Хранит партиции.
{% endtab %}

{% tab title="Продюсер" %}
Пишет сообщения.
{% endtab %}
{% endtabs %}

| Элемент | Назначение |
| ------- | ---------- |
| Topic   | Логический канал |
| Partition | Единица параллелизма |

</details>
