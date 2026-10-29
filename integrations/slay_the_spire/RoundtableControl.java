package roundtable;

import basemod.BaseMod;
import basemod.interfaces.PostInitializeSubscriber;
import basemod.interfaces.PostRenderSubscriber;
import com.badlogic.gdx.Gdx;
import com.badlogic.gdx.graphics.Pixmap;
import com.badlogic.gdx.graphics.PixmapIO;
import com.badlogic.gdx.graphics.g2d.SpriteBatch;
import com.badlogic.gdx.utils.ScreenUtils;
import com.google.gson.Gson;
import com.evacipated.cardcrawl.modthespire.lib.SpirePatch;
import com.evacipated.cardcrawl.modthespire.lib.SpireReturn;
import com.evacipated.cardcrawl.modthespire.lib.SpireInitializer;
import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.core.CardCrawlGame;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.helpers.TipTracker;
import com.megacrit.cardcrawl.screens.mainMenu.MenuButton;
import com.megacrit.cardcrawl.screens.options.ConfirmPopup;
import communicationmod.CommandExecutor;
import communicationmod.GameStateConverter;
import communicationmod.InvalidCommandException;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.HashMap;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;

/** Adds normal menu lifecycle actions and visible card text, never changes game rules. */
@SpireInitializer
public class RoundtableControl implements PostInitializeSubscriber, PostRenderSubscriber {
    private static boolean captureRequested = false;
    private static int captureId = 0;
    public static void initialize() { BaseMod.subscribe(new RoundtableControl()); }
    public void receivePostInitialize() {
        // Tutorial popups require mouse clicks and block the normal action interface.
        TipTracker.disableAllFtues();
    }
    public void receivePostRender(SpriteBatch sb) {
        if (!captureRequested) return;
        captureRequested = false;
        Pixmap pixels = null;
        try {
            // Read only this game's framebuffer, never the desktop or another window.
            sb.flush();
            Path folder = Paths.get(System.getenv("ROUNDTABLE_CAPTURE_DIR"));
            Files.createDirectories(folder);
            String name = "frame-" + (++captureId) + ".png";
            pixels = ScreenUtils.getFrameBufferPixmap(0, 0, Gdx.graphics.getWidth(), Gdx.graphics.getHeight());
            for (int i = 3; i < pixels.getPixels().limit(); i += 4) pixels.getPixels().put(i, (byte)255);
            PixmapIO.PNG encoder = new PixmapIO.PNG();
            try {
                encoder.setFlipY(true); // OpenGL's framebuffer starts at the lower edge.
                encoder.write(Gdx.files.absolute(folder.resolve(name).toString()), pixels);
            } finally {
                encoder.dispose();
            }
            HashMap<String, Object> data = new HashMap<>();
            data.put("file", name);
            data.put("floor", AbstractDungeon.floorNum);
            data.put("captured_at", System.currentTimeMillis());
            Files.write(folder.resolve("latest.tmp"), new Gson().toJson(data).getBytes("UTF-8"));
            Files.move(folder.resolve("latest.tmp"), folder.resolve("latest.json"), StandardCopyOption.REPLACE_EXISTING);
        } catch (Exception error) {
            System.err.println("Roundtable capture failed: " + error.toString());
        } finally {
            if (pixels != null) pixels.dispose();
        }
    }
    @SpirePatch(clz = CommandExecutor.class, method = "getAvailableCommands")
    public static class Available {
        public static ArrayList<String> Postfix(ArrayList<String> __result) {
            if (__result.contains("start")) {
                if (CardCrawlGame.characterManager.anySaveFileExists()) {
                    __result.remove("start"); // An existing run must be resumed, not overwritten.
                    __result.add("resume");
                }
                __result.add("quit");
            } else if (CommandExecutor.isInDungeon() && AbstractDungeon.player != null
                    && !AbstractDungeon.player.isDead) {
                __result.add("pause_run");
                if (System.getenv("ROUNDTABLE_CAPTURE_DIR") != null) __result.add("capture");
            }
            return __result;
        }
    }

    @SpirePatch(clz = CommandExecutor.class, method = "executeCommand")
    public static class Lifecycle {
        public static SpireReturn<Boolean> Prefix(String command) throws InvalidCommandException {
            command = command.trim().toLowerCase();
            if (!command.equals("resume") && !command.equals("pause_run") && !command.equals("quit") && !command.equals("capture")) {
                return SpireReturn.Continue();
            }
            if (!CommandExecutor.getAvailableCommands().contains(command)) {
                throw new InvalidCommandException("Lifecycle command unavailable: " + command);
            }
            try {
                if (command.equals("capture")) {
                    captureRequested = true;
                    return SpireReturn.Return(false);
                } else if (command.equals("resume")) {
                    for (MenuButton button : CardCrawlGame.mainMenuScreen.buttons) {
                        if (button.result == MenuButton.ClickResult.RESUME_GAME) {
                            button.buttonEffect();
                            return SpireReturn.Return(true);
                        }
                    }
                    throw new IllegalStateException("Continue button not ready");
                } else if (command.equals("pause_run")) {
                    // The game's own Save & Quit path uses its normal room checkpoint.
                    ConfirmPopup popup = new ConfirmPopup("", "", ConfirmPopup.ConfirmType.EXIT);
                    Method confirm = ConfirmPopup.class.getDeclaredMethod("yesButtonEffect");
                    confirm.setAccessible(true);
                    confirm.invoke(popup);
                    return SpireReturn.Return(true);
                } else {
                    Gdx.app.exit();
                    return SpireReturn.Return(false);
                }
            } catch (Exception error) {
                Throwable cause = error.getCause() == null ? error : error.getCause();
                throw new InvalidCommandException("Lifecycle command failed: " + cause.toString());
            }
        }
    }

    @SpirePatch(clz = GameStateConverter.class, method = "convertCardToJson")
    public static class CardText {
        public static HashMap<String, Object> Postfix(HashMap<String, Object> __result, AbstractCard card) {
            int damage = card.damage >= 0 ? card.damage : card.baseDamage;
            int block = card.block >= 0 ? card.block : card.baseBlock;
            __result.put("damage", damage);
            __result.put("block", block);
            __result.put("magic_number", card.magicNumber);
            __result.put("description", card.rawDescription.replace("!D!", Integer.toString(damage))
                    .replace("!B!", Integer.toString(block)).replace("!M!", Integer.toString(card.magicNumber))
                    .replace(" NL ", " "));
            return __result;
        }
    }
}
