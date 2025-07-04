from alpenstock.logging import LoguruInitalizer, logger


(
    LoguruInitalizer()
        .preset_brief()
        .set_level("INFO")
        .initialize()
)
logger.error("This is a brief error message.")
logger.warning("This is a brief warning message.")
logger.success("This is a brief success message.")
logger.info("This is a brief info message.")
logger.debug("This is a brief debug message.")
logger.trace("This is a brief trace message.")

# Re-initialize the logger will give a warning, then rewriting the previous
# configuration. But you can supress the warning by setting
# `warn_on_reinitialize` to False.
(
    LoguruInitalizer()
        .preset_full()
        .set_level("TRACE")
        # .serialize_to_file('test.log') # Optional: serialize logs to a file
        .initialize(on_reinitialize="overwrite")
)

logger.error("This is a full error message.")
logger.success("This is a full success message.")
logger.warning("This is a full warning message.")
logger.info("This is a full info message.")
logger.debug("This is a full debug message.")
logger.trace("This is a full trace message.")