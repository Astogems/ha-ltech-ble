# L Home Signer add-on image. Supervisor builds this when you install the add-on.
# The vendor library is NOT part of the image: it is read at runtime from
#   /share/lhome_signer/libmedia_pipeline.so   (you copy it there; see DOCS.md)

# ---- build the fat jar ----
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /src
COPY pom.xml .
COPY src ./src
RUN mvn -q clean package

# ---- runtime ----
FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /src/target/lhome-signer-jar-with-dependencies.jar /app/app.jar
ENV LHOME_SO=/share/lhome_signer/libmedia_pipeline.so
ENV LHOME_PORT=8777
# Inside the container we must listen on all interfaces so the published port works;
# real exposure is controlled by HA's port mapping + your LAN firewall.
ENV LHOME_BIND=0.0.0.0
EXPOSE 8777
CMD ["java","-jar","/app/app.jar"]
