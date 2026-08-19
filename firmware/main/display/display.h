#ifndef DISPLAY_H
#define DISPLAY_H

#include "emoji_collection.h"

#ifndef CONFIG_USE_EMOTE_MESSAGE_STYLE
#define HAVE_LVGL 1
#include <lvgl.h>
#endif

#include <esp_timer.h>
#include <esp_log.h>
#include <esp_pm.h>

#include <string>
#include <chrono>

class Theme {
public:
    Theme(const std::string& name) : name_(name) {}
    virtual ~Theme() = default;

    inline std::string name() const { return name_; }
private:
    std::string name_;
};

class Display {
public:
    Display();
    virtual ~Display();

    virtual void SetStatus(const char* status);
    virtual void ShowNotification(const char* notification, int duration_ms = 3000);
    virtual void ShowNotification(const std::string &notification, int duration_ms = 3000);
    virtual void SetEmotion(const char* emotion);
    virtual void SetChatMessage(const char* role, const char* content);
    virtual void ClearChatMessages();
    virtual void SetTheme(Theme* theme);
    virtual Theme* GetTheme() { return current_theme_; }
    virtual void UpdateStatusBar(bool update_all = false);
    virtual void SetPowerSaveMode(bool on);
    virtual void SetupUI() { 
        setup_ui_called_ = true;
    }

    inline int width() const { return width_; }
    inline int height() const { return height_; }
    inline bool IsSetupUICalled() const { return setup_ui_called_; }

protected:
    int width_ = 0;
    int height_ = 0;
    bool setup_ui_called_ = false;  // Track if SetupUI() has been called

    Theme* current_theme_ = nullptr;

    friend class DisplayLockGuard;
    virtual bool Lock(int timeout_ms = 0) = 0;
    virtual void Unlock() = 0;
};


// How long to wait for the display lock before giving up on an update.
//
// Short on purpose. Most of these updates run on the Application main
// task, which is the single serial path for MCP tool calls, the display
// tick, audio and every reply we send. Waiting here does not delay a
// redraw — it delays the entire device, and the task watchdog that now
// guards that loop fires at 30 s. A dropped frame is invisible; thirty
// seconds of a frozen robot is not.
#define DISPLAY_LOCK_TIMEOUT_MS 3000

class DisplayLockGuard {
public:
    DisplayLockGuard(Display *display) : display_(display) {
        locked_ = display_->Lock(DISPLAY_LOCK_TIMEOUT_MS);
        if (!locked_) {
            ESP_LOGE("Display", "Failed to lock display within %d ms; skipping update",
                     DISPLAY_LOCK_TIMEOUT_MS);
        }
    }
    ~DisplayLockGuard() {
        // Only release what we actually took. Unlocking a lock we never
        // acquired hands someone else's critical section away.
        if (locked_) {
            display_->Unlock();
        }
    }

    // Callers that touch LVGL directly should check this: without the
    // lock, doing so races whoever does hold it.
    bool locked() const { return locked_; }

private:
    Display *display_;
    bool locked_ = false;
};

class NoDisplay : public Display {
private:
    virtual bool Lock(int timeout_ms = 0) override {
        return true;
    }
    virtual void Unlock() override {}
};

#endif
