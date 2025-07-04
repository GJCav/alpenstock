from alpenstock.logging import LoguruInitalizer, logger


(
    LoguruInitalizer()
        .preset_brief()
        .set_level("INFO")
        .initialize()
)
logger.info("This is an info message.")

# Re-initialize the logger will give a warning, then rewriting the previous
# configuration. But you can supress the warning by setting
# `warn_on_reinitialize` to False.
(
    LoguruInitalizer()
        .preset_full()
        .set_level("DEBUG")
        .serialize_to_file('test.log') # Optional: serialize logs to a file
        .initialize(on_reinitialize="overwrite")
)
logger.debug("This is a debug message.")

logger.opt(colors=True).info("This is an info message with <fg>color</fg>.")