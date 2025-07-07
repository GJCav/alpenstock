# Basic usage

Alpenstock provides `LoguruInitalizer`, an convenient configurator for the
[loguru](https://github.com/Delgan/loguru).

Loguru is a powerful and flexible logging library for Python. The wrapper
provides a convenient way to configure the logger with different presets (mostly
based on my personal preferences).

## Initialize

``` py
from alpenstock.logging import LoguruInitalizer, logger

(
    LoguruInitalizer()
        .preset_brief()
        .set_level("TRACE")
        .initialize()
)

logger.error("This is a brief error message.")
logger.warning("This is a brief warning message.")
logger.success("This is a brief success message.")
logger.info("This is a brief info message.")
logger.debug("This is a brief debug message.")
logger.trace("This is a brief trace message.")
```

- Then you import the `logger` object by `from loguru import logger`. They are
  the same logger object.
- The default log level is `INFO`. Here we set it to `TRACE` to see all
  messages.



Screenshots of the output in a terminal:

<figure markdown="span">
  ![Light theme](_assets/logging%20demo%20(light).jpg){width="60%"}
  <figcaption>Light theme</figcaption>
</figure>

<figure markdown="span">
  ![Dark theme](_assets/logging%20demo%20(dark).jpg){width="60%"}
  <figcaption>Dark theme</figcaption>
</figure>

## Serialize messages to a file

``` py
(
    LoguruInitalizer()
        .preset_brief()
        .set_level("INFO")
        .serialize_to_file('test.log')
        .initialize()
)
```

## Re-initialize

Re-initialize the logger will give a warning, then rewriting the previous
configuration. But you can supress the warning by setting `on_reinitialize` to
`overwite`.

``` py
(
    LoguruInitalizer()
        .preset_full()
        .set_level("INFO")
        .initialize(on_reinitialize="overwrite")
)

logger.error("This is a full error message.")
logger.success("This is a full success message.")
logger.warning("This is a full warning message.")
logger.info("This is a full info message.")
logger.debug("This is a full debug message.")
logger.trace("This is a full trace message.")
```