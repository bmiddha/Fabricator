package io.github.bmiddha.fabricator;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import de.bluecolored.bluemap.api.BlueMapAPI;
import de.bluecolored.bluemap.api.markers.MarkerSet;
import de.bluecolored.bluemap.api.markers.POIMarker;
import net.blay09.mods.balm.api.Balm;
import net.blay09.mods.waystones.api.WaystoneTypes;
import net.blay09.mods.waystones.api.WaystoneVisibility;
import net.blay09.mods.waystones.api.WaystonesAPI;
import net.blay09.mods.waystones.api.event.WaystoneInitializedEvent;
import net.blay09.mods.waystones.api.event.WaystoneRemovedEvent;
import net.blay09.mods.waystones.api.event.WaystoneUpdatedEvent;
import net.blay09.mods.waystones.api.event.WaystonesLoadedEvent;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.minecraft.server.MinecraftServer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.Map;

public final class GlobalWaystones implements ModInitializer {
    private static final Logger LOG = LoggerFactory.getLogger("fabricator-global-waystones");
    private static final String LAYER = "fabricator:global-waystones";
    private static final Gson JSON = new Gson();
    private volatile BlueMapAPI api;
    private volatile boolean dirty = true;
    private int ticks;
    private String nonce;

    @Override
    public void onInitialize() {
        BlueMapAPI.onEnable(value -> { api = value; dirty = true; });
        BlueMapAPI.onDisable(value -> api = null);
        Balm.getEvents().onEvent(WaystonesLoadedEvent.class, event -> dirty = true);
        Balm.getEvents().onEvent(WaystoneInitializedEvent.class, event -> dirty = true);
        Balm.getEvents().onEvent(WaystoneUpdatedEvent.class, event -> dirty = true);
        Balm.getEvents().onEvent(WaystoneRemovedEvent.class, event -> dirty = true);
        ServerLifecycleEvents.SERVER_STARTED.register(server -> {
            nonce = readNonce();
            dirty = true;
        });
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> {
            BlueMapAPI current = api;
            if (current != null) {
                clear(current);
                try {
                    Files.deleteIfExists(current.getWebApp().getWebRoot().resolve("fabricator-status.json"));
                } catch (IOException error) {
                    LOG.error("Could not remove map heartbeat", error);
                }
            }
        });
        ServerTickEvents.END_SERVER_TICK.register(server -> {
            if (api == null || nonce == null) return;
            // Run after mutations on the server thread, not inside pre-change events.
            if (dirty || ++ticks >= 100) {
                dirty = false;
                ticks = 0;
                reconcile(server, api);
            }
        });
    }

    private String readNonce() {
        try {
            JsonObject config = JSON.fromJson(Files.readString(Path.of("config/fabricator-map.json")),
                    JsonObject.class);
            String value = config.get("nonce").getAsString();
            if (!value.matches("[a-f0-9]{64}")) throw new IllegalArgumentException("Invalid nonce");
            return value;
        } catch (IOException | RuntimeException error) {
            LOG.error("Private map setup is missing or invalid; global markers are disabled", error);
            return null;
        }
    }

    private static void clear(BlueMapAPI current) {
        for (var map : current.getMaps()) {
            map.getMarkerSets().remove(LAYER);
            // Balm's native integration is not visibility-filtered in this version.
            map.getMarkerSets().remove("waystones:waystones");
            map.getMarkerSets().remove("waystones:undiscovered_waystones");
            map.getMarkerSets().remove("waystones:sharestones");
        }
    }

    private void reconcile(MinecraftServer server, BlueMapAPI current) {
        try {
            var stones = WaystonesAPI.getAllWaystones(server).map(stone -> {
                var pos = stone.getPos();
                return new GlobalSnapshot.Stone(stone.getWaystoneUid().toString(),
                        stone.getDimension().location().toString(), stone.getEffectiveName().getString(),
                        stone.getVisibility() == WaystoneVisibility.GLOBAL,
                        stone.getWaystoneType().equals(WaystoneTypes.WAYSTONE),
                        stone.isValid(), stone.isTransient(), pos.getX(), pos.getY(), pos.getZ());
            }).toList();
            var desired = GlobalSnapshot.reconcile(stones);
            clear(current);
            JsonObject markerIdsByMap = new JsonObject();
            for (var level : server.getAllLevels()) {
                var dimensionStones = desired.getOrDefault(level.dimension().location().toString(), Map.of());
                current.getWorld(level.dimension()).ifPresent(world -> {
                    MarkerSet set = MarkerSet.builder().label("Global waystones").build();
                    JsonArray markerIds = new JsonArray();
                    for (var stone : dimensionStones.values()) {
                        markerIds.add(stone.id());
                        set.put(stone.id(), POIMarker.builder()
                                .label(stone.name())
                                .detail(GlobalSnapshot.escape(stone.name()))
                                .position(stone.x() + 0.5, stone.y(), stone.z() + 0.5).build());
                    }
                    for (var map : world.getMaps()) {
                        map.getMarkerSets().put(LAYER, set);
                        markerIdsByMap.add(map.getId(), markerIds);
                    }
                });
            }
            JsonObject status = new JsonObject();
            status.addProperty("schema", 1);
            status.addProperty("pid", ProcessHandle.current().pid());
            status.addProperty("nonce", nonce);
            status.addProperty("updated", Instant.now().getEpochSecond());
            status.addProperty("global_waystones", desired.values().stream().mapToInt(Map::size).sum());
            status.add("global_markers", markerIdsByMap);
            Path webroot = current.getWebApp().getWebRoot();
            Path temporary = webroot.resolve("fabricator-status.json.tmp");
            Files.writeString(temporary, JSON.toJson(status));
            Files.move(temporary, webroot.resolve("fabricator-status.json"),
                    StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException | RuntimeException error) {
            clear(current);
            LOG.error("Global-waystones reconciliation failed; private map heartbeat will expire", error);
        }
    }
}
